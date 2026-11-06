# tools/github/github_close.py — ends GitHub access for this sandbox. see _gateway.py next to this file

from . import _gateway


def run(agent):
    if agent.sandbox and _gateway.close_for(agent.sandbox):
        return "GitHub is closed for this sandbox"
    return "GitHub was not open in this sandbox"


TOOL = {
    "name": "github_close",
    "description": "End GitHub access for your sandbox, when you are done with GitHub.",
    "parameters": {"type": "object", "properties": {}},
}
