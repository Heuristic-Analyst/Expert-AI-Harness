# clients.py — finds and starts the clients: every .py file in clients/ is one, like every file in tools/ is a tool.
# a client file only needs a function start(hub). no list to add it to, nothing else to change.
# a client that doesn't work can't take the others down: it becomes a note, and the rest goes on.

import os
import importlib.util

FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clients")


def start_clients(hub):
    """call start(hub) of every client. returns how many said they run (start returned True)"""
    running = 0
    for file in sorted(os.listdir(FOLDER)):
        if not file.endswith(".py") or file.startswith("_"):   # _helpers.py: not a client
            continue
        try:
            spec = importlib.util.spec_from_file_location("clients." + file[:-3], os.path.join(FOLDER, file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if module.start(hub):
                running += 1
        except Exception as e:
            hub.emit("note", text="the client " + file + " could not start: " + type(e).__name__ + ": " + str(e))
    return running
