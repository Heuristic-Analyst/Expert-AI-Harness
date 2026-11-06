# tools/github

Real `git` and the whole GitHub API inside the sandbox, and no token in there.

| Tool | What it does |
|---|---|
| `github_open` | Starts the gateway for this sandbox, and sets git up to use it. |
| `github_close` | Ends it. It also ends with the sandbox. |

Then the agent uses GitHub like anyone would: `git clone https://github.com/...`, `git push`, `curl http://127.0.0.1:8484/api/...`.

Two switches, both yours, and both must be on:

| Command | What it does |
|---|---|
| `/github on` / `/github off` | GitHub allowed or not. **Off every time the program starts.** |
| `/sandbox net on` / `off` | The sandbox's network. GitHub follows it too. |

Either one off, and every request is refused at once, also in the middle of a session. The agent is told when you switch. It can't switch either one itself. `/github` works in the window, Telegram and `serve.py` (it is `clients/github_switch.py`). `main.py` doesn't have it, so there GitHub stays off.

## Why the token can't be found

A token inside a sandbox can always be read, however it is hidden: the agent is root in there. So it is not in there, at no point.

```
sandbox           git push  ->  127.0.0.1:8484  (a tiny listener, with no secret)
                                     |  through podman exec, only while the network is on
your computer     the gateway: drops every login in the request, adds the token
                                     |  only to github.com and api.github.com
GitHub
```

- The token is in `.env`, which no sandbox can see, and the gateway reads it for each request.
- It is added on your computer, so it never enters the sandbox: no file, no variable, no process in there has it.
- A request can't choose where it goes: there are two fixed hosts, and redirects aren't followed. So it can't be sent to a server the agent controls.
- Answers are searched for the token before they go back, also when it is cut in two between pieces.
- Web pages in the browser sandbox are refused, so a page can't use the gateway behind the agent's back.

The only way left to the token is to break out of the container itself, which is what Podman is there to stop.

## Setting it up

1. On GitHub: **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Repository access:** only the repos it should touch.
3. **Permissions:** what it may do. *Contents*, *Issues* and *Pull requests* read and write are enough for most work.
4. Put it into `.env`: `GITHUB_API_KEY=github_pat_...`
5. Start the program again. `python` has git, curl and python3; the sandbox needs python3 for this, and git to use git.

**Never give the agent your GitHub or email password.** GitHub doesn't take a password for this: the token is all there is.

## The token is the limit

The agent can't **know** the token, but it can **use** it: whatever the token allows, it can do, also if a web page or an issue tricks it. Force push, delete a branch, delete a repo if you gave it that. Nothing here stops that on purpose: the limit is what you allow when you make the token.

- **Keep it small.** Only the repos it needs, only the permissions it needs.
- **Leave *Workflows* off** unless it needs them: with it, it can run code on GitHub with your repo's secrets.
- **Leave *Administration* off:** that is deleting repos and changing their settings.
- **Protect `main` on GitHub** (Settings → Rules), so nothing reaches it without a pull request.

**The network switch covers it.** With `/sandbox net off` there is no GitHub either. Remove `GITHUB_API_KEY` from `.env` to switch it off for good. Note that the `browser` sandbox starts with its network on (its `sandbox.json`), so GitHub works there from the start.
