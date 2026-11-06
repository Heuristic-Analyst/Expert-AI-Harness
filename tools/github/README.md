# tools/github

Real `git` and the whole GitHub API inside the sandbox, and no token in there.

| Tool | What it does |
|---|---|
| `github_open` | Starts the gateway for this sandbox, and sets git up to use it. |
| `github_close` | Ends it. It also ends with the sandbox. |

Then the agent uses GitHub like anyone would: `git clone https://github.com/...`, `git push`, `curl http://127.0.0.1:8484/api/...`. It works with the sandbox's network off.

## Why the token can't be found

A token inside a sandbox can always be read, however it is hidden: the agent is root in there. So it is not in there, at no point.

```
sandbox           git push  ->  127.0.0.1:8484  (a tiny listener, with no secret)
                                     |  through podman exec, no network needed
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

**The sandbox's network doesn't cover this.** With `/sandbox net off` the agent still reaches GitHub once it called `github_open`, and only GitHub. Whoever has a token here accepts that: remove `GITHUB_API_KEY` from `.env` to switch all of it off.
