# clients

Every `.py` file in here is a client of the Hub (`interface/README.md`), started with the program by `gui.py` and `serve.py`. A new client is a new file: nothing else changes anywhere. Files starting with `_` are skipped.

## Telegram

Chat with the harness from your phone. The program must run on your computer (`python gui.py` or `python serve.py`). It needs nothing installed: the Bot API is plain https.

1. In Telegram, write to **@BotFather**, send `/newbot`, and follow it. It gives you a token.
2. Put it into `.env`: `TELEGRAM_API_KEY=<the token>`
3. Start the program, open your bot, and press **Start**. It answers with your user id.
4. Put that id into `settings/telegram.json` as `"Owner"`, and start the program again.
5. Press **Start** once more (or send `/start`). That message is the one that stays in the chat.

Only that account can use the bot; everyone else is ignored. It works in your private chat with the bot, not in groups.

| What you do | What happens |
|---|---|
| Write a message | It goes to the agent, and appears in every other client. |
| Write something in the window | It appears here as `You: ...`, with the answer below it. |
| Wait | One status message shows what the agent does right now: thinking, a tool call, its result. Each replaces the one before. When the answer comes the status is deleted. **Only answers stay.** |
| `/new`, or `/open` in any client | The chat is emptied except your `/start`, then that conversation is loaded: your messages and the answers, the newest 30 (`History`). |
| `/open` alone | The newest conversations as buttons. |
| A message while the agent works | It is dropped, not kept for later, and the bot says the agent is not ready. Send it again when it is done. Commands still work. |
| `/ping` | Answers if the program runs, for how long, and if the agent is free. No answer: it isn't running. |
| `/telegram off`, in any client | Nothing goes to this chat anymore. Stays off after a restart. |
| `/telegram on`, in any client | The chat is emptied and loads the open conversation, because a lot may have happened meanwhile. |

Good to know:

- **Text between tool calls is a status.** When the model says "let me check that" and then calls a tool, that text is replaced like any status. Only the final answer stays. The window shows all of it.
- **Older than 48 hours:** a bot can't delete those messages, Telegram doesn't allow it. They stay at the top of the chat, and it says so. Delete them by hand if they bother you.
- **Rate limit:** about one message per second per chat. Everything is sent by one thread with a gap between; status changes that come faster are skipped, answers never are. If Telegram still answers "too many requests", it waits as long as Telegram says. Loading a long conversation takes about a second per message.
- **While it is off** the bot only answers `/ping` and `/telegram on`. Anything else gets "Telegram is off", and doesn't reach the agent.
- **Messages sent while the program is off** are not run later: they stay in the chat, and nothing happens.
- **Message ids** are in `Conversations/.telegram.json`, because a bot can't ask Telegram what is in a chat. Delete that file and the bot forgets the old messages: they stay in the chat, and it can't delete them anymore.
- **Telegram sees everything** you and the agent say here, tool output too: bot chats aren't end-to-end encrypted. And whoever gets your token, or your Telegram account, can drive the agent.
- Only one running instance can have the bot.

## Making a new client

A file in here with a function `start(hub)` that returns `True` if it runs. It must not block: start your own thread. WhatsApp, Discord, a web page: all the same shape.

```python
# clients/echo.py - prints everything, and could send it anywhere
import threading

def start(hub):
    events = hub.subscribe()                       # every event, from every client

    def watch():
        while True:
            event = events.get()
            if event["type"] == "turn_end" and event["answer"]:
                print("the answer:", event["answer"])

    threading.Thread(target=watch, daemon=True).start()
    return True
```

To send something the user wrote: `hub.send(text, "echo")`. That is the whole connection. It returns `False` if the message was dropped because the agent is working: then tell your user.

A client can add a command of its own, which then works in every client: `hub.add_command("/echo", run, "  /echo   what it does")`. `run(words, source)` is called right away, so keep it quick. `/telegram` in `telegram.py` is an example.

The rules:

- **Don't draw your own action.** Call the Hub and wait for the event. Then you and every other client show the same thing.
- **Skip what you caused yourself** where it would appear twice: `user_message` and `command` carry `source`.
- **Use the final answer from `turn_end`**, not `text`: a `text` may be a remark before a tool call.
- **On a `conversation` event, empty your screen** and show `history` (or `hub.chat(history)` for a plain chat).
- **Never run a slow thing in `start`.** One client must not hold up the others.
- **Say who may use it.** A client that can be reached from outside can drive the agent: check who is writing, like `Owner` in `telegram.py`.
