# hub.py — the Hub: the one layer between the program and everything you talk to it with.
#
#   the program (agent, sandbox, tools, conversations)      not changed at all
#        |
#      the Hub                                              this file
#        |
#   the clients: the window (gui.py), Telegram (clients/telegram.py), a terminal (serve.py), whatever comes next
#
# a client only ever does two things:
#   - it CALLS the Hub: hub.send("hello", "telegram"), hub.open_conversation(name, "gui"), ...
#   - it READS what happens: q = hub.subscribe(), then q.get() again and again. every client gets EVERY event, in the
#     same order, no matter which client caused it. that is the whole trick: a client never updates itself after an
#     action. it asks the Hub, and draws what comes back, like every other client
# see interface/README.md for every call and every event.
#
# how it sees the program without changing it:
#   - WatchedAgent (below) is the normal Agent with two of its methods extended: print_part (the answer and thinking
#     while they stream in) and log (every finished message, with its tool calls and tool results)
#   - the program's modules get a print() of their own, so what they say (retries, checkpoints, "building ...") becomes
#     a note for every client instead of text in a terminal
#
# one job at a time: every turn of the agent and every change of conversation runs on one worker thread, in order. so
# two clients can never talk to the agent at once, and a conversation can't be swapped while the agent works in it.

import os
import re
import json
import queue
import atexit
import threading
import traceback

PROGRAM = "[from the program, not the user]"           # how the program's own messages to the model start (agent.py)
ERROR_SHOWN = 900                                      # characters of an error that are shown

HELP = """commands:
  /new                     start a new conversation
  /open <name>             continue a saved one
  /list                    show the saved conversations
  /rename <new>            rename this conversation
  /rename <old> <new>      rename another one
  /delete [name]           delete a conversation, with everything in it
  /sandbox                 what runs in the sandbox, and every sandbox
  /sandbox net on|off      the sandbox's internet
  /sandbox close           stop the sandbox
  /help                    this list"""


def watched(Agent, hub):
    """the program's Agent, plus: everything it does also goes to the Hub. nothing in classes/agent.py is changed.
    made here, and not at the top of this file, because importing Agent needs config.py and litellm"""

    class WatchedAgent(Agent):
        def print_part(self, kind, text):
            """instead of printing a piece of THINKING or ANSWER: hand it to the Hub"""
            if not text:
                return
            if kind != self.printing:                  # a new block starts
                self.printing = kind
                hub.emit("thinking_start" if kind == "THINKING" else "answer_start")
            hub.emit("thinking_piece" if kind == "THINKING" else "answer_piece", text=text)

        def log(self, message):
            """every finished message comes through here. so does its normal job: writing it into log.jsonl"""
            super().log(message)
            role = message.get("role")
            if role == "assistant":
                hub.emit("block_end")
                if (message.get("content") or "").strip():
                    hub.emit("text", text=message["content"])    # may still be followed by tools: see turn_end
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    hub.emit("tool_call", id=call.get("id"), name=function.get("name") or "?",
                             args=function.get("arguments") or "")
            elif role == "tool":
                hub.emit("tool_result", id=message.get("tool_call_id"), name=message.get("name") or "?",
                         result=message.get("content") or "")

    return WatchedAgent


class Hub:
    def __init__(self):
        self.guard = threading.RLock()         # one at a time: the listeners, the event counter, the job counter
        self.listeners = []                    # one queue per client
        self.count = 0                         # events so far: every event carries its number ("seq")
        self.jobs = queue.Queue()              # what the worker thread does next
        self.pending = 0                       # jobs waiting or running. 0 = free
        self.started = False                   # the harness is up
        self.agent = None                      # the agent of the open conversation
        self.current = None                    # the name of the open conversation
        self.counts = {}                       # conversation -> (its state.json's time and size, how many messages)
        self.view_hosts = {}                   # container -> its hostname ("" = unknown, None = being asked)

    # --- what happens: every client reads this ---
    def subscribe(self):
        """a new queue for one client. from now on every event lands in it, as a dict: {"type": ..., "seq": n, ...}"""
        events = queue.Queue()
        with self.guard:
            self.listeners.append(events)
        return events

    def unsubscribe(self, events):
        with self.guard:
            if events in self.listeners:
                self.listeners.remove(events)

    def emit(self, kind, **data):
        """tell every client that something happened. the lock keeps the order the same for all of them"""
        with self.guard:
            self.count += 1
            event = dict(data, type=kind, seq=self.count)
            for events in self.listeners:
                events.put(event)

    # --- starting: what main.py does when it starts ---
    def start(self):
        """start the harness. takes a while (podman): call it on a thread of your own if you have a window to keep
        alive. every problem becomes a "fatal" event. returns True if it runs"""
        self.emit("note", text="starting the harness…")
        try:
            import config
            from config import (SOUL_FILE, TOOLS_DIR, SKILLS_DIR, SANDBOXES_DIR, SHARED_DIR, COMMON, ENGINE,
                                INSTANCE, CONTAINER_CMD, SANDBOX_MEMORY, SANDBOX_CPUS, SANDBOX_PIDS,
                                COMMAND_TIMEOUT, MODEL, EFFORT, CONTEXT_LIMIT, READONLY_PROJECTS)
            from classes.agent import Agent
            from classes.sandbox import Sandbox
            from functions.read_text import read_text
            from functions.load_tools import load_tools
            from functions.load_skills import load_skills
            from functions import conversations, locks

            self.hook_modules()
            soul = read_text(SOUL_FILE) or "You are a helpful assistant."
            tools = load_tools(TOOLS_DIR)
            skills = load_skills(SKILLS_DIR)

            locks.start()
            atexit.register(locks.stop)                # runs last (atexit goes backwards)
            sandbox = Sandbox(SANDBOXES_DIR, SHARED_DIR, COMMON, ENGINE, INSTANCE, command=CONTAINER_CMD,
                              memory=SANDBOX_MEMORY, cpus=SANDBOX_CPUS, pids=SANDBOX_PIDS,
                              timeout=COMMAND_TIMEOUT, readonly=READONLY_PROJECTS)
            atexit.register(sandbox.shutdown)
            workspace = None
            if sandbox.available():
                workspace = sandbox
                sandbox.cleanup()
            else:                                      # no podman: no tools or skills that need it (see main.py)
                for name in [n for n, tool in tools.items()
                             if tool["folder"].split("/")[0] in ("sandbox", "notebook", "browser")]:
                    tools.pop(name)
                for name in ("sandbox", "notebook", "browser"):
                    skills.pop(name, None)
            if not skills:
                tools.pop("load_skill", None)

            self.Agent = watched(Agent, self)
            self.conv = conversations
            self.api_key = config.API_KEY
            self.soul, self.tools, self.skills = soul, tools, skills
            self.sandbox, self.workspace = sandbox, workspace
            self.model, self.effort, self.limit = MODEL, EFFORT, CONTEXT_LIMIT
            self.current = conversations.new_name()
            self.agent = self.new_agent(self.current)
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            self.emit("fatal", text="could not start the harness:\n\n" + traceback.format_exc())
            return False

        threading.Thread(target=self.worker, daemon=True).start()
        self.started = True
        self.emit("ready", model=self.model, limit=self.limit, tools=len(tools), skills=len(skills),
                  name=self.current, sandboxes=sandbox.names() if workspace else None)
        return True

    def hook_modules(self):
        """every module of the program that prints gets its own print(), which comes to the Hub"""
        import sys
        for name in ("classes.agent", "classes.sandbox", "functions.commands", "functions.conversations"):
            module = sys.modules.get(name)
            if module is not None:
                module.print = self.printed

    def printed(self, *args, sep=" ", end="\n", file=None, flush=False):
        """what print() is inside the program: a whole line becomes a note. tool calls already came through log()"""
        text = (sep if sep is not None else " ").join(str(a) for a in args).strip()
        if text and not text.startswith(("TOOL CALL: ", "TOOL RESULT: ")):
            self.emit("note", text=text)

    def new_agent(self, name, messages=None):
        """an agent for one conversation, like main.py makes one, but always streaming: clients show the answer and
        the thinking while they arrive"""
        conv = self.conv
        return self.Agent(self.model, self.api_key, instructions=self.soul, stream=True, effort=self.effort,
                          print_thinking=True, tools=self.tools, skills=self.skills, conversation=messages,
                          log_file=conv.log_of(name), results_dir=conv.results_of(name),
                          checkpoint_file=conv.checkpoint_of(name), context_limit=self.limit,
                          sandbox=self.workspace)

    # --- the worker: one job at a time ---
    def worker(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except BaseException:
                self.emit("error", text=traceback.format_exc()[-ERROR_SHOWN:])
            with self.guard:
                self.pending -= 1
                if self.pending == 0:
                    self.emit("busy", on=False)

    def add_job(self, job):
        with self.guard:
            self.pending += 1
            if self.pending == 1:
                self.emit("busy", on=True)
            self.jobs.put(job)

    def alone(self, job):
        """a job that changes the conversation: only while nothing else runs or waits. False = not now, and every
        client is told"""
        with self.guard:
            if not self.started:
                return False
            if self.pending:
                self.emit("reply", text="one moment: the agent is still working in " + str(self.current))
                return False
            self.add_job(job)
            return True

    def busy(self):
        """is the agent working (or is something waiting for it)?"""
        return self.pending > 0

    # --- talking to it: the one call most clients need ---
    def send(self, text, source):
        """anything the user typed, in any client. source: who you are, e.g. "telegram". a text starting with / is a
        command, anything else a message for the agent. a message sent while the agent works comes right after it"""
        text = str(text).strip()
        if not text:
            return
        if text.startswith("/"):
            self.command(text, source)
        elif not self.started:
            self.emit("reply", text="one moment: the harness is still starting")
        else:
            with self.guard:
                waiting = self.pending > 0
                self.add_job(lambda: self.turn(text, source))
            if waiting:
                self.emit("reply", text="the agent is working: your message comes right after")

    def turn(self, text, source):
        """one message to the agent, on the worker thread"""
        self.emit("user_message", text=text, source=source)
        self.emit("turn_start", source=source)
        agent, name = self.agent, self.current
        answer = None
        try:
            notes = self.workspace.take_notes() if self.workspace else []   # what the user changed with /sandbox
            if notes:
                text = "\n".join(notes) + "\n\n" + text
            answer = agent.ask(text)                   # the final answer: the message that came without tool calls
        except BaseException as e:
            self.emit("error", text=(type(e).__name__ + ": " + str(e))[:ERROR_SHOWN])
        finally:
            try:
                self.conv.save(name, agent)
            except BaseException:
                self.emit("error", text="could not save the conversation:\n" + traceback.format_exc()[-ERROR_SHOWN:])
            self.emit("block_end")
            self.emit("turn_end", answer=answer or None)

    # --- commands: the same in every client ---
    def command(self, text, source):
        """one /command, typed in any client. every client sees that it was typed, and what came of it"""
        words = text.split()
        what = words[0].lower()
        self.emit("command", text=text, source=source)
        if not self.started:
            self.emit("reply", text="one moment: the harness is still starting")
        elif what == "/new":
            self.new_conversation(source)
        elif what == "/open":
            if len(words) == 2:
                self.open_conversation(words[1], source)
            else:
                self.emit("reply", text="usage: /open <name>   (/list shows them)")
        elif what == "/list":
            self.emit("reply", text=self.list_text())
        elif what == "/rename":
            if len(words) == 2:
                self.rename_conversation(self.current, words[1], source)
            elif len(words) == 3:
                self.rename_conversation(words[1], words[2], source)
            else:
                self.emit("reply", text="usage: /rename <new name>   or   /rename <old> <new>")
        elif what == "/delete":
            self.delete_command(words, source)
        elif what == "/sandbox":
            self.sandbox_command(words)
        elif what == "/help":
            self.emit("reply", text=HELP)
        elif what in ("/exit", "/quit"):
            self.emit("reply", text="that only works on the computer itself")
        else:
            self.emit("reply", text="command not found: " + words[0] + "    (try /help)")

    def delete_command(self, words, source):
        """/delete [name] only asks. /delete [name] yes deletes: there is no undo, and every client has a keyboard"""
        words = words[1:]
        sure = words[-1:] == ["yes"]
        if sure:
            words = words[:-1]
        name = words[0] if words else self.current
        if not self.conv.exists(name):
            self.emit("reply", text="no conversation called '" + str(name) + "'. /list shows them")
        elif sure:
            self.delete_conversation(name, source)
        else:
            self.emit("reply", text="delete '" + name + "' with everything in it? there is no undo. to do it, send:\n"
                                    "/delete " + name + " yes")

    def sandbox_command(self, words):
        if not self.workspace:
            self.emit("reply", text="podman is not installed or not running, so there are no sandboxes")
        elif len(words) == 1:
            self.in_background(self.workspace.overview)
        elif len(words) == 3 and words[1] == "net" and words[2] in ("on", "off"):
            self.in_background(lambda: self.workspace.user_network(words[2] == "on"))
        elif len(words) == 2 and words[1] == "close":
            self.in_background(self.workspace.user_close)
        elif len(words) == 2 and words[1] == "shell":
            self.emit("reply", text="the interactive root shell only works in main.py in a terminal")
        else:
            self.emit("reply", text="usage: /sandbox   |   /sandbox net on|off   |   /sandbox close")

    def in_background(self, ask):
        """something for the sandbox: on its own thread, so it also works while the agent is working"""
        def work():
            try:
                self.emit("reply", text=ask())
            except BaseException:
                self.emit("error", text=traceback.format_exc()[-ERROR_SHOWN:])
            self.emit("status")
        threading.Thread(target=work, daemon=True).start()

    # --- the conversations ---
    def saved(self):
        """every saved conversation, oldest first"""
        return self.conv.names()

    def conversations(self):
        """every saved conversation, plus the open one if nothing was said in it yet"""
        names = self.saved()
        if self.current and self.current not in names:
            names = names + [self.current]
        return names

    def exists(self, name):
        return self.conv.exists(name)

    def pretty(self, name):
        """a conversation as one line: its time, and how many messages it has"""
        shown = name
        found = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})-(\d{2})-(\d{2})", name)
        if found:
            shown = "%s-%s-%s  %s:%s" % found.groups()[:5]
        try:
            path = self.conv.state_of(name)
            stat = os.stat(path)
            key = (stat.st_mtime, stat.st_size)
            known = self.counts.get(name)
            if known and known[0] == key:
                count = known[1]
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    count = f.read().count('"role"')   # one per message, no json needed
                self.counts[name] = (key, count)
            if count:
                shown += "   · " + str(count)
        except OSError:                                # nothing saved yet
            pass
        return shown

    def list_text(self):
        names = self.saved()
        lines = ["saved conversations:"]
        for name in names:
            lines.append(("  * " if name == self.current else "    ") + name + "   (" + self.pretty(name) + ")")
        if not names:
            lines.append("  none yet")
        return "\n".join(lines)

    def new_conversation(self, source):
        """start a new conversation. the one you leave is saved already"""
        return self.alone(lambda: self.go_new(source))

    def go_new(self, source, why="new"):
        self.current = self.conv.new_name()
        self.agent = self.new_agent(self.current)
        self.emit("conversation", why=why, name=self.current, history=[], source=source)

    def open_conversation(self, name, source):
        """continue a saved conversation, in every client"""
        if not self.started:
            return False
        if not self.conv.exists(name):
            self.emit("reply", text="no conversation called '" + str(name) + "'. /list shows them")
            return False
        if name == self.current:
            self.emit("reply", text=name + " is the open conversation")
            return False
        return self.alone(lambda: self.go_open(name, source))

    def go_open(self, name, source):
        data = self.conv.load(name)                    # what the model carries right now (and its size)
        messages = data.get("messages") or []
        agent = self.new_agent(name, list(messages))
        agent.tokens = data.get("tokens", 0)
        history = self.history(name)                   # everything ever said, which is more than that
        self.current, self.agent = name, agent
        self.emit("conversation", why="opened", name=name, source=source,
                  history=history if history is not None else messages)

    def rename_conversation(self, old, new, source):
        return self.alone(lambda: self.go_rename(old, new, source))

    def go_rename(self, old, new, source):
        problem = self.conv.rename(old, new)
        if problem:
            self.emit("reply", text=problem)
            return
        if old == self.current:                        # the one we are in: its files live under the new name now
            self.current = new
            self.agent.log_file = self.conv.log_of(new)
            self.agent.results_dir = self.conv.results_of(new)
            self.agent.checkpoint_file = self.conv.checkpoint_of(new)
        self.emit("renamed", old=old, new=new, name=self.current, source=source)
        self.emit("reply", text="renamed " + old + " -> " + new)

    def delete_conversation(self, name, source):
        """delete a conversation for good. it does NOT ask: a client that offers this asks first (/delete does)"""
        return self.alone(lambda: self.go_delete(name, source))

    def go_delete(self, name, source):
        problem = self.conv.delete(name)
        if problem:
            self.emit("reply", text=problem)
            return
        self.emit("reply", text="deleted " + name)
        if name == self.current:                       # the one we are in: start a new one, or the next message
            self.go_new(source, why="deleted")         # would bring it back
        else:
            self.emit("list_changed")

    # --- reading a conversation ---
    def history(self, name):
        """everything ever said in a conversation, out of log.jsonl (which is only ever added to), as a list of
        messages. None if there is no log. this is more than state.json has: a checkpoint empties the conversation to
        make room, but never the log. a checkpoint shows up as {"role": "_checkpoint", "content": its text}"""
        path = self.conv.log_of(name)
        if not os.path.isfile(path):
            return None
        entries = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue                           # a half written line (the program died mid write)
                kind = item.get("kind")
                if kind == "system":
                    text = item.get("content") or ""
                    if "<checkpoint>" in text:         # the conversation was emptied here: show the note
                        start = text.find("<checkpoint>") + len("<checkpoint>")
                        end = text.find("</checkpoint>")
                        entries.append({"role": "_checkpoint", "content": text[start:end].strip() if end > start
                                        else text})
                elif kind == "message":
                    message = item.get("message") or {}
                    if message.get("role"):
                        entries.append(message)
        return entries

    def chat(self, history):
        """a history as a plain chat, for a client that only shows messages: [{"role": "user" or "assistant",
        "text": ...}]. no tools, no thinking, none of the program's own messages, only what you and the answers said"""
        said = []
        for message in history or []:
            text = message.get("content") if isinstance(message.get("content"), str) else ""
            if message.get("role") == "user":
                if text.startswith(PROGRAM):           # a note from the program, maybe with your message after it
                    head, gap, rest = text.partition("\n\n")
                    if not gap or not all(line.startswith(PROGRAM) for line in head.split("\n")):
                        continue
                    text = rest
                said.append({"role": "user", "text": text})
            elif message.get("role") == "assistant" and text.strip() and not message.get("tool_calls"):
                said.append({"role": "assistant", "text": text})
        return said

    # --- how things stand ---
    def tokens(self):
        """how big the open conversation is right now, in tokens"""
        if self.agent is None:
            return 0
        try:
            return self.agent.size()
        except BaseException:
            return getattr(self.agent, "tokens", 0) or 0

    def sandbox_line(self):
        """one line: what runs in the sandbox"""
        try:
            return self.workspace.status_line() if self.workspace else "no sandbox"
        except BaseException:
            return "no sandbox"

    def browser_view(self):
        """the link to watch the browser, only while it is open, or None. reads Browser/tmp/open.json (see browser.py)
        and compares the hostname in it, so a file left behind by an older sandbox doesn't count. podman is asked off
        this thread, once per container"""
        sandbox = self.workspace
        if not sandbox:
            return None
        url, container, project = sandbox.view_url(), sandbox.container, sandbox.mounted("Browser")
        if not url or not container or not project:
            return None
        if container not in self.view_hosts:
            self.view_hosts[container] = None

            def ask():
                code, out = sandbox.podman("inspect", "--format", "{{.Config.Hostname}}", container, timeout=60)
                self.view_hosts[container] = out.strip() if code == 0 else ""
            threading.Thread(target=ask, daemon=True).start()
        host = self.view_hosts.get(container)
        if not host:                                   # still asking, or podman couldn't say
            return None
        try:
            with open(os.path.join(sandbox.shared_dir, project, "tmp", "open.json"), encoding="utf-8") as f:
                return url if json.load(f).get("host") == host else None
        except (OSError, ValueError, AttributeError):  # no file (closed), or half written: not open (yet)
            return None

    def save(self):
        """save the open conversation, e.g. when leaving"""
        if self.agent is not None and self.current:
            self.conv.save(self.current, self.agent)
