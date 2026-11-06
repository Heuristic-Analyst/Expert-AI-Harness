# tools/github/github_open.py — makes git and the GitHub API work inside the sandbox. see _gateway.py next to this file

import subprocess

from . import _gateway


def in_sandbox(sandbox, *command):
    return subprocess.run([sandbox.command, "exec", sandbox.container] + list(command), capture_output=True, timeout=60)


def run(agent):
    sandbox = agent.sandbox
    if not sandbox:
        return "there is no sandbox on this computer"
    if not sandbox.running():
        return sandbox.not_running()
    if not _gateway.token():
        return ("GitHub is not set up: there is no GITHUB_API_KEY in .env on the user's computer. tell the user "
                "(tools/github/README.md). never ask them for a token or password in the chat")
    if not sandbox.network:
        return ("the sandbox's network is off, and GitHub follows it. only the user can switch it on: ask them to "
                "send /sandbox net on, then call github_open again")
    if in_sandbox(sandbox, "sh", "-c", "command -v python3").returncode != 0:
        return "this sandbox has no python3, which GitHub needs here. close it and open another one (e.g. python)"
    problem = _gateway.open_for(sandbox)
    if problem:
        return problem
    base = "http://127.0.0.1:" + str(_gateway.PORT)
    has_git = in_sandbox(sandbox, "sh", "-c", "command -v git").returncode == 0
    if has_git:
        key = "url." + base + "/.insteadOf"
        in_sandbox(sandbox, "git", "config", "--global", "--unset-all", key)
        for address in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
            in_sandbox(sandbox, "git", "config", "--global", "--add", key, address)
    return ("GitHub works in this sandbox now, as long as its network is on. git as usual: git clone "
            "https://github.com/<owner>/<repo>, git push, git pull. the API: any path under " + base + "/api/, e.g. "
            "curl -s " + base + "/api/user" + ("" if has_git else ". this sandbox has no git, only the API works")
            + ". there is no token in here and you need none: never look for one. after the sandbox restarts, call "
            "github_open again")


TOOL = {
    "name": "github_open",
    "description": "Make GitHub work inside your sandbox: real git (clone, pull, push) and the whole REST API, with the "
                   "user's login added for you outside the sandbox. Works with the sandbox's network off. Call it again "
                   "after the sandbox restarted. Load the github skill first.",
    "parameters": {"type": "object", "properties": {}},
}
