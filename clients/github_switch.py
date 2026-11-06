# clients/github_switch.py — /github on|off: the user's switch for GitHub (tools/github). not a chat client: it only adds
# this one command to the Hub, the same way telegram.py adds /telegram, so it works in the window, Telegram and serve.py.
#
# the switch is kept in the program on your computer (on the sandbox object), where the agent can't reach it: a tool
# can't send a command, and nothing in the sandbox can change it. it starts OFF every time the program starts.
# while it is off, the gateway refuses every request, and github_open refuses to start.

NAME = "/github"


def start(hub):
    sandbox = hub.workspace
    if sandbox is None:                                # no podman: no sandbox, so no GitHub either
        return False
    sandbox.github_allowed = False                     # off at every start

    def switch(words, source):
        if len(words) != 2 or words[1] not in ("on", "off"):
            hub.emit("reply", text="github is " + ("on" if sandbox.github_allowed else "off")
                                   + ". usage: /github on|off")
            return
        wanted = words[1] == "on"
        if wanted == sandbox.github_allowed:
            hub.emit("reply", text="github is already " + words[1])
            return
        sandbox.github_allowed = wanted
        sandbox.notes.append("[from the program, not the user] The user switched GitHub " + (
            "ON: github_open works now (the sandbox's network must be on too)." if wanted else
            "OFF: every GitHub request is refused now, and github_open won't start. only the user can switch it on."))
        hub.emit("reply", text="github is " + words[1] + (" (the sandbox's network must be on too: /sandbox net on)"
                                                          if wanted and not sandbox.network else ""))

    hub.add_command(NAME, switch, "  /github on|off           let the agent use GitHub, or don't (off at every start)")
    return True
