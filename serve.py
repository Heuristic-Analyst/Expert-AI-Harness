# serve.py — the harness without a window: the Hub, every client in clients/ (Telegram), and this terminal as one more
# client. for when your phone is all you need. python serve.py, /exit or ctrl+c leaves.
# it is also the smallest example of a client: it reads the events, it prints them, it hands your lines to the Hub.

import sys
import threading

from interface.hub import Hub
from interface.clients import start_clients

NAME = "terminal"


def show(events):
    """print what happens, whoever caused it"""
    while True:
        event = events.get()
        kind = event["type"]
        if kind in ("note", "reply", "error"):
            print(event["text"])
        elif kind == "ready":
            print("model: " + event["model"] + " | conversation: " + event["name"] + "   (/help for commands)")
        elif kind == "fatal":
            print(event["text"])
        elif kind in ("user_message", "command") and event["source"] != NAME:
            print("\n> " + event["text"])
        elif kind == "answer_piece":
            print(event["text"], end="", flush=True)
        elif kind == "block_end":
            print()
        elif kind == "tool_call":
            print("[" + event["name"] + "]")
        elif kind == "conversation":
            print("\nconversation: " + event["name"])


def main():
    hub = Hub()
    threading.Thread(target=show, args=(hub.subscribe(),), daemon=True).start()
    if not hub.start():
        sys.exit(1)
    start_clients(hub)
    try:
        while True:
            text = input().strip()
            if text.lower() in ("/exit", "/quit"):
                break
            hub.send(text, NAME)
    except (KeyboardInterrupt, EOFError):
        pass
    hub.save()


if __name__ == "__main__":
    main()
