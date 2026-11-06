# interface

The layer between the program and everything you talk to it with.

```
the program        agent, sandbox, tools, conversations      not changed
     |
the Hub            interface/hub.py                          this folder
     |
the clients        gui.py, clients/telegram.py, serve.py     as many as you like, at the same time
```

| File | What it is |
|---|---|
| `hub.py` | The `Hub`: starts the harness, runs one job at a time, and tells every client everything that happens. |
| `clients.py` | Finds every file in `clients/` and starts it. |

A client does two things, and only these:

1. It **calls** the Hub: `hub.send("hello", "telegram")`.
2. It **reads** events: `events = hub.subscribe()`, then `events.get()` in a loop.

Every client gets every event, in the same order, whoever caused it. So a client never draws its own action: it asks the Hub, and draws what comes back, like all the others. That is why a message typed in Telegram appears in the window, and `/new` in the window empties Telegram. To write a client, see `clients/README.md`.

`main.py` doesn't use the Hub: it is the plain terminal version, and stays as it was. `serve.py` is the terminal as a client.

## Calls

`source` is who you are, like `"telegram"`: it travels with the events, so the others can show where something came from.

| Call | What it does |
|---|---|
| `send(text, source)` | **The one most clients need.** Anything the user typed: a text starting with `/` is a command, anything else a message for the agent. Returns `False` if the message was dropped: while the agent works nothing is kept for later. Only the client that sent it knows, and it tells its user. |
| `subscribe()` | A new queue, where every event lands from now on. `unsubscribe(queue)` ends it. |
| `new_conversation(source)` | A new conversation, in every client. |
| `open_conversation(name, source)` | Continue a saved one, in every client. |
| `rename_conversation(old, new, source)` | Rename one. |
| `delete_conversation(name, source)` | Delete one, with no question asked. For a client that asks itself; `send("/delete x")` asks, `send("/delete x yes")` does it. |
| `saved()`, `conversations()`, `exists(name)` | The saved conversations; the same plus the open one; is there one with this name. |
| `pretty(name)` | A conversation as one line: its time, and how many messages it has. |
| `history(name)` | Everything ever said in it, as messages, also across checkpoints. |
| `chat(history)` | A history as a plain chat, without tools and thinking: `[{"role": "user" or "assistant", "text": ...}]`. |
| `busy()`, `tokens()`, `sandbox_line()`, `browser_view()`, `current` | How things stand: the agent is working; the size of the conversation; what runs in the sandbox; the link to the browser's screen while it is open; the open conversation. |
| `ping()` | One line: the program runs, for how long, and whether the agent is free. |
| `add_command(word, run, line)` | Add a command of your own, like `/telegram`. It works in every client. `run(words, source)`, `line` is for `/help`. |
| `save()` | Save the open conversation, when leaving. |
| `start()` | Starts the harness. `gui.py` and `serve.py` do this; a client in `clients/` never needs to. |

## Commands

The same in every client, through `send`: `/new`, `/open <name>`, `/list`, `/rename [old] <new>`, `/delete [name]`, `/sandbox`, `/sandbox net on|off`, `/sandbox close`, `/ping`, `/help`, plus what clients add (`/telegram on|off`, `/github on|off`).

While the agent works, `/new`, `/open`, `/rename` and `/delete` are refused, with a reply saying so: a conversation can't be swapped while the agent works in it. The others work at any time, also `/sandbox net off`.

## Events

Each one is a dict with `type` and `seq` (its number), plus what the table says.

| `type` | Also has | What happened |
|---|---|---|
| `ready` | `model`, `limit`, `tools`, `skills`, `name`, `sandboxes` | The harness is up. `sandboxes` is `None` without Podman. |
| `fatal` | `text` | The harness could not start. |
| `busy` | `on` | The agent started working, or everything is done. |
| `user_message` | `text`, `source` | A message goes to the agent now. |
| `command` | `text`, `source` | Someone typed a command. |
| `turn_start` | `source` | The agent starts on a message. |
| `thinking_start`, `thinking_piece` | `text` (the piece) | The model's thinking, streaming in. |
| `answer_start`, `answer_piece` | `text` (the piece) | The model's text, streaming in. |
| `block_end` | | That text is complete. |
| `text` | `text` | One whole text of the model. It may still be followed by tools, so it isn't the answer yet: see `turn_end`. |
| `tool_call` | `id`, `name`, `args` | The agent calls a tool. |
| `tool_result` | `id`, `name`, `result` | A tool's result, shortened as the model sees it. |
| `turn_end` | `answer` | The turn is over. `answer` is the final answer, or `None` if it failed. |
| `reply` | `text` | The answer to a command. |
| `note` | `text` | Something the program says by itself: a retry, a checkpoint, `building ...`. |
| `error` | `text` | Something went wrong. The program goes on. |
| `conversation` | `why`, `name`, `history`, `source` | Another conversation is open now. `why`: `new`, `opened`, or `deleted` (the open one was deleted, this is a new one). Empty your screen and show `history`. |
| `renamed` | `old`, `new`, `name`, `source` | A conversation was renamed. `name`: the open one. |
| `list_changed` | | The list of conversations changed. |
| `status` | | The sandbox changed: ask `sandbox_line()` again. |

## How it sees the program without changing it

- `WatchedAgent` in `hub.py` is the normal `Agent` with two methods extended: `print_part` (the text while it streams in) and `log` (every finished message, with its tool calls and results). If those two are ever renamed in `classes/agent.py`, change them here too.
- The program's modules get their own `print()`, so what they say becomes a `note`.

Every turn and every change of conversation runs on one worker thread, one after the other. So two clients can never talk to the agent at once.
