# Claude Code on DataOS

`apps/claude-code`: one page with a browser terminal (ttyd + tmux) running Claude Code with `dapp` inside, and next to it an on-demand
browser that you and Claude share (see [The browser](#the-browser)). Ask it to build an app; it writes
`app/` and `dapp.yaml` in `~/work`, deploys with `dapp deploy` (see [run-from-source.md](run-from-source.md)), checks the app, and
gives you the URL. URL: `https://poliks.instance.dataos.cloud/claude-code/`.

## Everyone deploys their own (dashboard → Claude Code)
The **Claude Code** tab deploys a personal instance in one click, like the Models tab: `claude-<your id>`, private to you, running as you. Optional
**Customize** (compute, CPU and memory request/limit, defaults 500m/4 CPU and 2560Mi/6Gi, room for the browser) with a live cost estimate (about $0.8/day reserved; DataOS doesn't bill these
apps, so it is a comparison), and an optional **Claude token** field. The page then shows its state (starting → running), **Open Claude Code**, whether you are
signed in to Claude, and:
- **Stop (keep my files):** deletes only the app, so the pod stops costing. Your code and conversations stay in your Storage (`workspace`, `claude-sessions`) and the
  stored token stays; **Deploy** again restores them.
- **Stay signed in:** paste the output of `claude setup-token` once. It becomes the DataOS secret `dapp-claude-<you>-anthropic` (only your instance reads it, never
  shown again; saving restarts a running instance).
- **Everyone's Claude Code:** a table of who has one and in what state (what DataOS lets you list); only the owner can open one.
- **Delete:** the app and its secrets (token, sign-in); the workspace in Storage stays until you delete it there.

Why one per person and not one shared app: the pod runs as one DataOS user and everyone in a shell shares its Linux user, so a shared instance would deploy with the
owner's rights and let people read each other's files and Claude login. The cost of that isolation is the idle reservation, which **Stop** removes.
Two instances of the same person (this tab's and an older hand-deployed `claude-code`) would sync the same workspace object: keep one.

## The machine: terminal, shared desktop, previews, its own disk
The `claude-code` app is the owner's own machine. The page is split: the terminal (Claude Code) on the left, a desktop on the right
(drag the divider; **Hide desktop** gives the terminal the whole width).

**Desktop.** Openbox on Xvfb `:99` with Chromium; right-click the desktop for a terminal (xterm) or the file manager (pcmanfm). You see
and use it through noVNC; Claude uses the same screen two ways:
- `browser` MCP (Playwright over CDP): web pages, precise, doesn't move the real mouse;
- `desktop` MCP (`desktop_mcp.py`): `screenshot` of the whole screen, and `click` / `type_text` / `key` / `scroll` / `wait` with the
  real mouse and keyboard (xdotool), for dialogs, desktop apps, or to see what you are doing.

You stay in charge: while Claude moves the mouse or types, an orange bar says **Claude is using the mouse and keyboard**. **Take over**
makes the desktop tool refuse every mouse and keyboard action (screenshots still work) until you press **Give back to Claude**.
Sign in to sites on the screen yourself; Claude works in that session without seeing the password.

It runs only when needed: it starts with **Start desktop**, or by itself the first time Claude uses the browser or the desktop tool,
and stops after 15 minutes with nobody watching it and Claude not using it (`BROWSER_IDLE_MINUTES`). Chromium's profile is in
`$HOME/.browser`: on the machine's disk its sign-ins survive restarts (stale lock files from a hard stop are removed at start).

**Previews.** A server running in the machine on a port 1024-32767 opens at `https://<host>/claude-code/port/<port>/`, behind the same
owner-only sign-in; the header lists running ports under **Preview**. The app sees requests at `/`, so it needs relative links or a
base path. The machine's own ports (8080, 7680, 7681, 9222, 9223, 5900, 9102 metrics) and DataOS's sidecar ports (their sockets aren't held by the machine's processes) can't be previewed or listed.

**Disk.** A DataOS Volume `claude-code-home` (20 Gi, `apps/claude-code/home.volume.yaml`) is mounted at `/data`, and `HOME` is
`/data/home` (`DAPP_HOME_DIR`). Everything under it survives restarts and deploys: code, `.git`, virtualenvs, `node_modules`, the
browser's sign-ins, the Claude sign-in. On the first start with the disk, `~/work` and the Claude conversations are restored from
immortal-obj once (marker `~/.dapp-restored`); after that the disk is the truth and immortal-obj keeps a backup copy every 30 s. If
the disk is missing or not writable, `start.sh` falls back to `/tmp/claude-home` (the old behaviour). Only one pod can mount the
Volume; when DataOS moves the pod to another node, reattaching takes 30-60 s. No root, no `apt`, no Docker: tools are installed as
the user (`pip --user`, `uv`, `npm`) onto the disk.

How: `workbench.py` (aiohttp, on 127.0.0.1:7680 behind `dapp_proxy`) serves the page, passes `/term` to ttyd and `/port/<n>/` to the
app on that port (Content-Encoding passed through), serves noVNC under `/browser/vnc/`, bridges `/browser/ws` to x11vnc, and listens
for CDP on 127.0.0.1:9222 (starting Xvfb, Openbox, Chromium with its CDP on 9223, and x11vnc on 5900 at the first connection).
Everything listens on localhost only. `/desktop/takeover` and `/desktop/handback` create and remove `/tmp/desktop-paused`; the desktop
tool touches `/tmp/desktop-claude-active` on each action, which the page shows for 10 s.

## Who can open it, and as whom
Private (DataOS sign-in), and `dapp_proxy` additionally lets only the **owner** through. The terminal acts with the owner's
DataOS identity (DataOS gives the pod `DATAOS_RUN_AS_USER` / `DATAOS_RUN_AS_APIKEY`), so everything it deploys is yours and
DataOS permissions apply. It has a shell: treat the page like a terminal on your account.

## DataOS tools in online Claude Code (dapp-monitor by default)

Every start registers the gateway tool **`dapp-monitor`** for Claude (`start.sh`, `DAPP_CLAUDE_TOOLS`, default `dapp-monitor`;
space-separated names, `""` for none). Claude then has `list_resources`, `status`, `recent_logs`, `search_logs` (any time range,
old pods included) and `log_summary`, for apps and services ([dapp-monitor.md](dapp-monitor.md)). `dapp tool connect`
registers a small stdio bridge (`dapp tool bridge dapp-monitor`, httpx only: no MCP SDK, no venv) that signs every call with
this pod's DataOS identity, so the gateway checks that you may use the tool and records each call. A tool that is not on the
gateway, or that you may not use, is skipped with a line in the app's log.

Before an image with this default, or to switch by hand, in the online terminal (`/exit` from Claude first):

```bash
claude mcp remove --scope user dapp
dapp tool connect dapp-monitor
claude mcp list
claude --continue
```

The old setup (a venv with `mcp<2` running `dapp mcp`) is no longer needed: `rm -rf ~/.venvs/dapp-mcp` removes it. To try it,
ask Claude:

> Use dapp-monitor: is chatbot running, and were there any errors in its logs in the last 24 hours? Quote the lines.

The full `dapp` MCP (deploy, scale, secrets, access) still exists as `dapp mcp` (it works with MCP SDK 2.x from claude-code
0.1.13); online, Claude usually doesn't need it, because it runs the `dapp` CLI directly.

## What survives
| Thing | Survives a browser refresh | Survives a pod restart or move |
|---|---|---|
| The running Claude session | yes (tmux reattaches) | no |
| Everything under `$HOME` (code, `.git`, venvs, browser sign-ins) | yes | **yes, on the machine's disk** (`/data/home`) |
| `~/work` files without the disk | yes | yes, saved to `user:<you>/workspace` every 30 s and at shutdown (not caches, venvs, `.git`, `.env`/keys, databases, files over 700 KB) |
| Claude conversations | yes | yes, `claude-sessions` (`claude --continue`), when the save worked |
| Claude sign-in | yes | **only with a token secret** (below) |
| Deployed apps | n/a | yes, they have their own code |

## Staying signed in
`/login` is stored under `/tmp` in the pod. DataOS replaces pods now and then (node moves, rollouts), and every deploy of
claude-code replaces the pod, so you are asked to sign in again. Fix it once with a token:

1. `claude setup-token` (anywhere you have Claude Code; needs a Claude subscription). It prints a token valid for a year.
2. Store it as a DataOS secret without it touching a file you keep or a chat:
   ```bash
   T=$(pbpaste | tr -d ' \n\r'); printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$T" > /tmp/cc.env \
     && dapp secret set claude-code anthropic --from-env-file /tmp/cc.env --no-attach; rm -f /tmp/cc.env; unset T; printf '' | pbcopy
   ```
   (select the token, copy it, then run this; it clears the clipboard after.)
3. `secrets: [dapp-claude-code-anthropic]` in `apps/claude-code/dapp.yaml` (already there), `dapp deploy -f apps/claude-code/dapp.yaml`.

`start.sh` then skips the first-run screens. An `ANTHROPIC_API_KEY` in the same secret works too (Claude Code reads it through
`apiKeyHelper`, so `env` does not show it). A token is visible in the terminal's environment to anything running there: do not run untrusted code in it.
Never paste a token in chat or commit it. To replace it, run `claude setup-token` again and repeat step 2.

## What the agent knows (CLAUDE.md)
`apps/claude-code/CLAUDE.md` is copied to `~/work` on every start and is what the online Claude follows. It opens with **recipes**: a **low-cost chatbot**
(pick a model from `dapp model catalog`, `dapp model deploy qwen2.5-0.5b --name small`, `dapp init NAME --template chatbot --model small`, `dapp deploy`, check the
302, report URL, model and estimated cost), **chat over data products** (the Data Product MCP tool names and the flow for a model that can't call tools), and a
custom app. Then the reference: apps from source, OpenAI and in-DataOS models, immortal-obj, sharing and access requests, commands, how to finish. A test
(`tests/test_cli_model.py`) checks that every `dapp ...` command in it exists, so the guide can't drift from the CLI. It only matches the CLI of the image it ships in:
rebuild the image after changing either.

## Resources
"M": `resources: {cpu: "1", memory: 4Gi, cpuLimit: "2", memoryLimit: 6Gi}`, plus the 20 Gi disk. Claude alone peaks near 340 MiB and
0.1 core over a day; the desktop and Chromium add ~1-1.5 GiB while they run, and the rest leaves room for a dev server or a build.
Estimated with dapp's rates: about $1.5/day reserved, about $2/month for the disk. Resources do not cause logouts.

## Files
`start.sh` (sets `HOME` to the disk or `/tmp/claude-home`, restores the workspace once, registers the browser and desktop MCPs, starts
ttyd, the workbench and the proxy), `workbench.py` + `workbench.html` (the split page, the on-demand desktop, Take over, previews),
`desktop_mcp.py` (the desktop tool), `home.volume.yaml` (the disk), `session.sh` (tmux attach),
`claude-loop.sh` (`claude --continue`, then a shell), `workspace-sync.py` (restore at start, save every `DAPP_SYNC_SECONDS`
and on SIGTERM; a save only starts after a successful restore, so a failed restore never wipes the saved copy),
`CLAUDE.md` (the guide the agent reads, copied to `~/work` on every start), `tmux.conf`.

## Troubleshooting
- **Asked to sign in after a refresh:** the pod was replaced. Check `dapp --json status claude-code` for a new pod name; set up the token above.
- **`[workspace-sync] could not save …` in `dapp logs claude-code`:** the gateway or immortal-obj was not reachable at that moment (timeout or
  connection refused). It retries every 30 s; a save that never succeeds means `~/work` is only in the pod.
- **`claude-sessions: 0 files` after a restart:** nothing had been saved from the earlier pod.
- **Deploy from the pod fails with `owner is required`:** the pod writes through the REST API (no `dataos-ctl`). The API writer sets the owner and encodes secret
  values since claude-code 0.1.5; on an older image, rebuild. Test the pod's path from the laptop with `DAPP_WRITER=api uv run dapp deploy -f dapp.yaml`.
- **The browser says "stopped by itself":** Chromium ran out of memory. Close heavy tabs, or raise the memory limit in Customize.
- **Build:** `docker buildx build --platform linux/amd64 -f apps/claude-code/Dockerfile -t docker.io/tmdcio/dapp-claude-code:0.1.13 --push .`
  then bump the tag in `dapp.yaml` and `src/dapp/claude.py` (`CLAUDE_CODE_IMAGE`).
