# clients/telegram.py — talk to the harness from your phone, through a Telegram bot. one client of the Hub
# (interface/hub.py): it changes nothing in the program, it only calls the Hub and reads its events.
# how to set it up: clients/README.md. needs nothing but python: the Bot API is plain https.
#
# what you see in the chat:
#   - what you type in any other client appears as "You: ..."
#   - while the agent works there is ONE status message: thinking, then a tool call, then its result, ... each one
#     replaces the one before. when the answer comes the status is deleted. only answers stay
#   - opening a conversation (or a new one), from any client, empties the chat and loads that conversation
#   - a message sent while the agent works is dropped, and it says so
#   - /telegram off, from any client: nothing goes to this chat anymore, until /telegram on
#
# what Telegram doesn't allow, and how this works with it:
#   - a bot can't read a chat, so it keeps every message id itself: Conversations/.telegram.json
#   - about one message per second per chat, or it answers 429 "wait n seconds". so one thread sends everything, one
#     thing at a time, with a gap. status messages that come faster are skipped, only the newest is shown. answers are
#     never skipped, and a 429 is waited out
#   - a bot can't delete a message older than 48 hours. those stay in the chat, and it says so
#   - an empty chat throws you out to the "Start" button, so the last /start is never deleted
#
# only your own account can use it: "Owner" in settings/telegram.json. everyone else is ignored.

import os
import re
import html
import json
import time
import threading
import collections
import urllib.error
import urllib.request

from config import ENV_FILE, CONVERSATIONS_DIR, HERE
from functions import locks
from functions.environment import read_key
from functions.read_json import read_json
from functions.write_text import write_text

NAME = "telegram"                                      # who we are, for the Hub
API = "https://api.telegram.org"
SETTINGS = os.path.join(HERE, "settings", "telegram.json")
STATE = os.path.join(CONVERSATIONS_DIR, ".telegram.json")   # the message ids. a file, so it is not a conversation

GAP = 1.05                                             # seconds between two things sent: Telegram's one per second
LONGEST = 3800                                         # characters per message. Telegram's limit is 4096, and <b> counts
SHORT = 220                                            # characters of a tool call or result in the status message
BATCH = 100                                            # messages one deleteMessages call takes
TOO_OLD = 48 * 3600                                    # seconds after which a bot can't delete a message
PICKER = 10                                            # conversations /open offers as buttons
MENU = [("new", "start a new conversation"), ("open", "continue a saved conversation"),
        ("list", "show the saved conversations"), ("sandbox", "what runs in the sandbox"),
        ("ping", "is the program running"), ("telegram", "on or off: send to this chat or not"),
        ("help", "all commands")]
NOT_READY = "⏳ The agent is not ready, it is still working. This message was not sent."
IS_OFF = "Telegram is off. /telegram on turns it on."


def start(hub):
    """the Hub's clients.py calls this. no token, or no settings: this client simply doesn't run"""
    token = read_key("TELEGRAM", ENV_FILE)
    if not token:
        return False
    settings = read_json(SETTINGS) if os.path.isfile(SETTINGS) else {}
    if not locks.take("telegram"):                     # one bot, one program: Telegram allows only one reader anyway
        hub.emit("note", text="telegram: another running instance has the bot, so this one doesn't use it")
        return False
    Telegram(hub, token, settings).run()
    return True


def shorten(text, limit=SHORT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def cut(text, limit=LONGEST):
    """cut a long text into messages, at empty lines or line ends where possible"""
    parts = []
    while len(text) > limit:
        at = text.rfind("\n\n", 0, limit)
        if at < limit // 2:
            at = text.rfind("\n", 0, limit)
        if at < limit // 2:
            at = limit
        parts.append(text[:at].rstrip())
        text = text[at:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


def inline(text):
    """**bold**, `code`, # headers and [links](https://...), on text that is html-escaped already"""
    pieces = re.split(r"(`[^`\n]+`)", text)
    for i, piece in enumerate(pieces):
        if i % 2:                                      # `code`: nothing else counts inside it
            pieces[i] = "<code>" + piece[1:-1] + "</code>"
        else:
            piece = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", piece)
            piece = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", piece, flags=re.MULTILINE)
            pieces[i] = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)\"]+)\)", r'<a href="\2">\1</a>', piece)
    return "".join(pieces)


def to_html(text):
    """the model's markdown, as the small html Telegram understands. if Telegram rejects it anyway, say() sends the
    plain text"""
    pieces = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    for i, piece in enumerate(pieces):
        if i % 2:                                      # a ``` block. its first line may name the language
            code = re.sub(r"^[^\n]*\n", "", piece[3:-3], count=1)
            pieces[i] = "<pre>" + html.escape(code, quote=False) + "</pre>"
        else:
            pieces[i] = inline(html.escape(piece, quote=False))
    return "".join(pieces)


class Telegram:
    def __init__(self, hub, token, settings):
        self.hub = hub
        self.url = settings.get("ApiUrl", API) + "/bot" + token + "/"
        self.owner = int(settings.get("Owner") or 0)   # your own user id. 0 = not set yet: it tells you your id
        self.shown = int(settings.get("History", 30))  # how many messages of a conversation are loaded when it opens
        self.every = float(settings.get("StatusEvery", 2))   # seconds between two changes of the status message
        self.started = time.time()
        self.events = hub.subscribe()

        self.ledger = threading.Lock()                 # the poller and the sender both change the two below
        self.first = None                              # the message that is never deleted: the last /start
        self.ids = []                                  # every other message in the chat: [id, when it was sent]
        self.on = True                                 # False: /telegram off, nothing goes to the chat
        self.load()

        self.signal = threading.Condition()            # what the sender does next:
        self.out = collections.deque()                 # things that must happen, in this order
        self.wanted = None                             # the newest status text. overwritten, so fast ones are skipped
        self.status = None                             # the id of the status message in the chat
        self.status_at = 0                             # when it last changed
        self.last_write = 0                            # when anything was last sent
        self.working = False                           # a turn of the agent is running
        self.last_text = None                          # the last text of the model in this turn

    def run(self):
        if not self.owner:
            self.hub.emit("note", text="telegram: no Owner set yet. write to your bot: it answers with your id. put "
                                       "that into settings/telegram.json as \"Owner\" and start again")
        else:
            self.hub.add_command("/telegram", self.switch, "  /telegram on|off         send to Telegram, or don't")
            if self.on:
                self.queue(self.say, "Harness is on. New conversation: " + str(self.hub.current))
        for job in (self.poll, self.send_loop, self.watch):
            threading.Thread(target=job, daemon=True).start()

    # --- the message ids. a bot can't ask Telegram for them, so they must survive a restart ---
    def load(self):
        try:
            with open(STATE, encoding="utf-8") as f:
                saved = json.load(f)
            self.first, self.ids, self.on = saved.get("first"), saved.get("ids", []), saved.get("on", True)
        except (OSError, ValueError):                  # no file yet, or a broken one: start without
            pass

    def save(self):
        write_text(STATE, json.dumps({"first": self.first, "ids": self.ids, "on": self.on}))

    def remember(self, message_id, when=None):
        with self.ledger:
            self.ids.append([message_id, when or int(time.time())])
            self.save()

    def forget(self, message_id):
        with self.ledger:
            self.ids = [item for item in self.ids if item[0] != message_id]
            self.save()

    # --- talking to Telegram ---
    def call(self, method, wait=30, **params):
        """one call to the Bot API. returns (True, its result) or (False, what Telegram said). a 429 is waited out, a
        lost connection tried again, as long as it takes: an answer must not get lost because the wifi dropped"""
        delay = 2
        data = json.dumps(params).encode("utf-8")
        while True:
            request = urllib.request.Request(self.url + method, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=wait + 10) as response:
                    return True, json.loads(response.read().decode("utf-8")).get("result")
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8"))
                except ValueError:
                    body = {}
                if e.code == 429:                      # too fast: Telegram says how long to wait
                    time.sleep(int(body.get("parameters", {}).get("retry_after", 5)) + 1)
                    continue
                if e.code >= 500:
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                return False, str(body.get("description", "error " + str(e.code)))
            except (OSError, ValueError):              # no connection, a timeout, a broken answer
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def say(self, text, styled=False, buttons=None):
        """send one text, in as many messages as it takes. styled: it is the model's markdown. these messages stay"""
        for part in cut(text):
            params = {"chat_id": self.owner, "text": part}
            if buttons:
                params["reply_markup"] = {"inline_keyboard": buttons}
            if styled:
                ok, sent = self.write("sendMessage", parse_mode="HTML", **dict(params, text=to_html(part)))
                if not ok:                             # the markdown was too odd for Telegram: send it as it is
                    ok, sent = self.write("sendMessage", **params)
            else:
                ok, sent = self.write("sendMessage", **params)
            if ok:
                self.remember(sent["message_id"])
            else:
                self.hub.emit("note", text="telegram could not send a message: " + sent)

    def write(self, method, **params):
        """one call that puts something in the chat or takes it away: never faster than one per GAP seconds. only the
        sender thread uses this, so nothing can overtake"""
        left = self.last_write + GAP - time.time()
        if left > 0:
            time.sleep(left)
        result = self.call(method, **params)
        self.last_write = time.time()
        return result

    # --- the sender: the only thread that sends, so the order and the rate stay right ---
    def queue(self, job, *args, **more):
        with self.signal:
            self.out.append((job, args, more))
            self.signal.notify()

    def show_status(self, text):
        with self.signal:
            self.wanted = text
            self.signal.notify()

    def next_job(self):
        """what to do next: first what must happen, then the status, when it is time for it"""
        with self.signal:
            while True:
                if self.out:
                    return self.out.popleft()
                if self.wanted is None:
                    self.signal.wait()
                    continue
                left = self.status_at + self.every - time.time()
                if left > 0:
                    self.signal.wait(left)
                    continue
                text, self.wanted = self.wanted, None
                return self.update_status, (text,), {}

    def send_loop(self):
        while True:
            job, args, more = self.next_job()
            try:
                job(*args, **more)
            except Exception as e:                     # one failed message must not end the client
                self.hub.emit("note", text="telegram: " + type(e).__name__ + ": " + str(e))

    def update_status(self, text):
        """the one status message: changed in place (one call, no new message, no sound)"""
        self.status_at = time.time()
        if self.status:
            ok, said = self.write("editMessageText", chat_id=self.owner, message_id=self.status, text=text)
            if ok or "not modified" in said:
                return
            self.forget(self.status)                   # it is gone (e.g. you deleted it): send a new one
            self.status = None
        ok, sent = self.write("sendMessage", chat_id=self.owner, text=text, disable_notification=True)
        if ok:
            self.status = sent["message_id"]
            self.remember(self.status)

    def drop_status(self):
        if self.status:
            self.write("deleteMessage", chat_id=self.owner, message_id=self.status)
            self.forget(self.status)
            self.status = None

    def clear(self):
        """delete everything in the chat but the first message. Telegram skips ids it can't find"""
        self.status = None
        with self.ledger:
            ids, self.ids = self.ids, []
            self.save()
        now = time.time()
        old = sum(1 for _, when in ids if now - when >= TOO_OLD)
        numbers = [number for number, _ in ids]
        for start in range(0, len(numbers), BATCH):
            self.write("deleteMessages", chat_id=self.owner, message_ids=numbers[start:start + BATCH])
        if old:
            self.say(str(old) + " older message(s) stayed above: a bot can't delete anything older than 48 hours")

    def load_chat(self, event):
        """the header of a conversation that was just opened, and its messages"""
        said = self.hub.chat(event["history"])
        left_out = max(0, len(said) - self.shown)
        title = ("New conversation: " if event["why"] != "opened" else "Conversation: ") + event["name"]
        self.say(title + ("\n(" + str(left_out) + " earlier messages not shown)" if left_out else ""))
        for message in said[left_out:]:
            if message["role"] == "user":
                self.say("You: " + message["text"])
            else:
                self.say(message["text"], styled=True)

    # --- /telegram on|off, from any client ---
    def switch(self, words, source):
        if len(words) != 2 or words[1] not in ("on", "off"):
            self.hub.emit("reply", text="telegram is " + ("on" if self.on else "off") + ". usage: /telegram on|off")
            return
        wanted = words[1] == "on"
        if wanted == self.on:
            self.hub.emit("reply", text="telegram is already " + words[1])
            return
        with self.ledger:
            self.on = wanted
            self.save()
        with self.signal:                              # what was waiting is not sent anymore, or is out of date
            self.out.clear()
            self.wanted = None
        if wanted:                                     # things happened while it was off: start from what is true now
            self.queue(self.clear)
            self.queue(self.load_chat, {"why": "opened", "name": self.hub.current,
                                        "history": self.hub.history(self.hub.current)})
        else:
            self.queue(self.drop_status)
            self.queue(self.say, IS_OFF)               # the last thing it says, so the phone knows why it is quiet
        self.hub.emit("reply", text="telegram is " + words[1])

    # --- what happens in the harness: the same events every client gets ---
    def watch(self):
        while True:
            event = self.events.get()
            try:
                if event["type"] in ("turn_start", "turn_end"):   # also while off: it may be switched on mid-turn
                    self.working = event["type"] == "turn_start"
                if self.owner and self.on:
                    self.on_event(event)
            except Exception as e:
                self.hub.emit("note", text="telegram: " + type(e).__name__ + ": " + str(e))

    def on_event(self, event):
        kind = event["type"]
        if kind in ("user_message", "command"):
            if event["source"] != NAME:                # your own message is in the chat already
                self.queue(self.say, "You: " + event["text"])
        elif kind == "turn_start":
            self.last_text = None
            self.show_status("💭 Working…")
        elif kind == "thinking_start":
            self.show_status("💭 Thinking…")
        elif kind == "answer_start":
            self.show_status("✍️ Writing…")
        elif kind == "text":                           # could be the answer, or a remark before a tool call. only
            self.last_text = event["text"]             # turn_end knows. until then it is a status like any other
            self.show_status("💬 " + shorten(event["text"]))
        elif kind == "tool_call":
            self.show_status("🔧 " + event["name"] + ": " + shorten(event["args"]))
        elif kind == "tool_result":
            self.show_status("📄 " + event["name"] + " result: " + shorten(event["result"]))
        elif kind == "note":
            if self.working:
                self.show_status("ℹ️ " + shorten(event["text"]))
            else:
                self.queue(self.say, event["text"])
        elif kind == "reply":
            self.queue(self.say, event["text"])
        elif kind == "error":
            self.queue(self.say, "⚠️ " + event["text"])
        elif kind == "turn_end":
            with self.signal:
                self.wanted = None                     # nothing older may come after the answer
            self.queue(self.drop_status)
            answer = event["answer"] or self.last_text
            if answer:
                self.queue(self.say, answer, styled=True)
        elif kind == "conversation":
            with self.signal:
                self.out.clear()                       # what was waiting belongs to the chat that is being emptied
                self.wanted = None
            self.queue(self.clear)
            self.queue(self.load_chat, event)

    # --- what you type: a long poll for new updates ---
    def poll(self):
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU])
        offset = None
        while True:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset:
                params["offset"] = offset
            ok, updates = self.call("getUpdates", wait=35, **params)
            if not ok:                                 # e.g. another program reads this bot (409)
                self.hub.emit("note", text="telegram: " + str(updates))
                time.sleep(30)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    if "message" in update:
                        self.on_message(update["message"])
                    elif "callback_query" in update:
                        self.on_button(update["callback_query"])
                except Exception as e:
                    self.hub.emit("note", text="telegram: " + type(e).__name__ + ": " + str(e))

    def on_message(self, message):
        user = (message.get("from") or {}).get("id")
        if not self.owner:                             # not set up yet: nothing reaches the agent, it only says the id
            self.call("sendMessage", chat_id=message["chat"]["id"],
                      text="Your user id is " + str(user) + ". Put it into settings/telegram.json as \"Owner\", then "
                           "start the program again.")
            return
        if user != self.owner or message["chat"]["id"] != self.owner:   # only you, only your private chat with the bot
            return
        text = message.get("text") or ""
        number, when = message["message_id"], message.get("date", int(time.time()))
        if text.split()[:1] == ["/start"] or self.first is None:
            with self.ledger:                          # the newest /start is the one that is kept. the old one may go
                if self.first is not None:
                    self.ids.append([self.first, when])
                self.first = number
                self.save()
        else:
            self.remember(number, when)
        if when < self.started - 30:                   # sent while the program was off: not run hours later
            return
        word = text.split()[0].lower() if text.split() else ""
        if not self.on:                                # off: only what answers the phone, or turns it on again
            if word == "/telegram":
                self.hub.send(text, NAME)
            elif word == "/ping":
                self.queue(self.say, self.hub.ping())
            else:
                self.queue(self.say, IS_OFF)
        elif word == "/start":
            self.queue(self.say, "Connected. Just write. /help shows the commands.")
        elif not text:
            self.queue(self.say, "Only text messages work here for now.")
        elif word == "/open" and len(text.split()) == 1:
            self.queue(self.pick)
        elif text.strip() and not self.hub.send(text, NAME):
            self.queue(self.say, NOT_READY)

    def pick(self):
        """/open with nothing after it: the newest conversations as buttons. nobody types a timestamp on a phone"""
        names = self.hub.saved()[-PICKER:][::-1]
        if not names:
            self.say("no saved conversations yet")
            return
        self.say("Open which conversation?", buttons=[[{"text": self.hub.pretty(name), "callback_data": "open:" + name}]
                                                      for name in names])

    def on_button(self, query):
        self.call("answerCallbackQuery", callback_query_id=query["id"])
        data = query.get("data") or ""
        if (query.get("from") or {}).get("id") == self.owner and data.startswith("open:"):
            self.hub.open_conversation(data[len("open:"):], NAME)
