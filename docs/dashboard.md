# Dashboard

`https://poliks.instance.dataos.cloud/dapp/` (`deploy/dashboard.dapp.yaml`, image `dataos-apps`). Any DataOS user may sign in;
every call it makes runs as that person, so DataOS permissions decide what they see. API clients (the `dapp` CLI, agents) use
`/api/*` with an API key or token. Sessions are in memory, so it runs one replica.

## Pages
| Page | What |
|---|---|
| **Apps** (home) | Your apps (or all in the tenant), status, owner, who can open each, CPU, memory, the **secrets** (names only, linked to the app's Secrets tab) and **Volumes** (id and mount, or a disk, or "none: files are lost on restart") each app uses, LLM tokens. KPI "Access requests" and a pill per app when people are waiting |
| App → Overview | Status, replicas, CPU and memory now (with 24 h graphs), requests, LLM tokens, pods, deployment (image, port, compute, **volumes**, secrets). For an app that serves a model (llm-small, `model-…`): **"who used it"**, the apps that called it |
| App → LLM usage | Gateway key (rotate, revoke), usage by user, by day and model |
| App → Secrets, Access, Logs, Settings | Manage secrets, the allow list and requests, read logs, scale or delete |
| **Storage** | immortal-obj as you (see below) |
| **Models** | Deploy, use and delete small language models: [models.md](models.md) |
| **Claude Code** | Deploy your own Claude Code in a click, stop and start it, store your Claude token: [claude-code.md](claude-code.md) |
| Tools | MCP tools behind the gateway: who can use them, usage |
| Keys | DataOS API keys and gateway keys |

## Storage
Lists your objects (kind, updated, who they are shared with) and objects shared with you, such as your chat history from the chatbot.
Open an object to:
- **database:** browse tables and rows (`_kv` first), run SQL (one statement; the same guards as the tool), save a key.
- **volume:** list files, read a text file, save a file.
- **share / unshare** with a person or `app:<name>` (read or write); **delete** (type the name to confirm).
"Try it" on the Storage page saves a first value or file in one step. Read-only shares hide the write forms.
The page talks to the gateway as you (your token), so you only see what immortal-obj allows you.

## Access requests
Someone without access opens a private app and gets the **No access** page, which now has **Request access**.
That links to `/dapp/request-access?app=<app>`: sign in if needed, add an optional note (300 characters), send.
- Stored in the dashboard's own object `access-requests` (needs `objects: true` on the dashboard, which it has).
- The **owners** of the app see the request on its Access tab (and on the home page). **Approve** does what
  `dapp access allow APP USER` does (the app restarts, about 40 s); **Decline** grants nothing.
- Only an owner may decide (checked against the app's owners in DataOS); the person to allow is read from the stored
  request, not from the form; a request is decided once. One open request per person per app, 20 open per person.
- No email or chat notification yet: the owner sees it on the dashboard.
- The link appears only for apps running the newer `dapp_login` (runtime `r8`, chatbot 0.1.12 or later) and a known app name
  (`DAPP_APP_NAME`, set by `dapp deploy`; for older apps it is taken from a single-segment path). Redeploy older apps.

## Compute numbers
DataOS does not collect CPU/memory for app namespaces, so apps report their own (`metrics: true` runs `dapp_metrics`, port 9102).
Series are filtered by the app's DataOS **service name**, not only `job_name`, because other people's apps with the same name
(for example another `chatbot`) would otherwise be added into your numbers. An app with no running pod, or one that does not
report metrics (immortal-obj has none), shows `–`. A personal LLM key (`user:<you>`) has no compute.

## Look and feel
Sticky header with the current page marked, light and dark themes, tables that scroll on phones. Templates are in
`src/dapp_dashboard/templates/`; the dashboard has no build step.

## Run and deploy
```bash
dapp deploy -f deploy/dashboard.dapp.yaml        # image bump in the file first
DAPP_DEV=1 uv run dapp-dashboard                  # local, uses your dataos-ctl login
```
