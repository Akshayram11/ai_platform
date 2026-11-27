# Next tasks: data that survives, GitHub, tools for agents

Written 2026-11-27, after dataplane `poldp01` was removed from poliks and every app, service, compute and Volume on it
went with it. The apps are back on `poliksdp01` (commit `1bed793`), but the Volumes came back empty: the gateway's keys,
usage and budgets, every immortal-obj object (chat history, Agent Builder drafts, Claude's workspace backup) and
Claude Code's home are gone. Nothing had a copy outside that dataplane.

## What went wrong

| # | Problem | Effect today |
|---|---|---|
| 1 | All state lived on DataOS Volumes on one dataplane, with no backup (Volumes have no snapshots) | Keys, usage, budgets, objects, agent configs, Claude's home: lost |
| 2 | Code written by Claude Code and Agent Builder configs were only in `~/work` and immortal-obj | Lost with the Volumes |
| 3 | `poliksdp01` can't reach `files.pythonhosted.org` | Source apps with `requirements.txt` hang at start; worked around with images |
| 4 | Recovery was manual: Volumes, 7 apps in order, tools, keys, access | Took about an hour of hand steps, written down nowhere |
| 5 | Gateway settings (registered tools, allow lists, budgets) exist only in its database | Re-registered by hand |
| 6 | Agent Builder and some dashboard changes are not committed | Agents page is not live |

## Work, in order

### 1. Backups outside DataOS: not doing (decided 2026-11-27)
Agent configs and other app state stay on the immortal-obj Volume; no backup job for now. The rest of this section is
kept as the design if it is picked up later.

- Nightly job (a DataOS Workflow) that dumps the gateway database and the immortal-obj store, compresses them and copies
  them to object storage outside the dataplane (S3 bucket or a Lakehouse Depot). Keep 14 daily copies.
- `dapp backup ls` and `dapp backup restore gateway|immortal-obj --from DATE`.
- Alert (dapp-monitor or the dashboard) when the last backup is older than 36 hours.
- Done when: we delete a test Volume, restore it from the backup, and the gateway's keys and objects are back.

### 2. Connect GitHub (each user's own account)
One connection per user, made once in the dashboard, used by Claude Code and Agent Builder.

- Admin, once: create a GitHub App "DataOS Workspace" with device flow on, permission Contents read/write, installable
  on any account. Put its Client ID (public) in the dashboard's config. No client secret needed for device flow.
- User flow: **Connect GitHub** → code like `WXYZ-1234` and `github.com/login/device` → authorize → pick one repo
  ("Only select repositories") or create a private `claude-workspace` → "Connected as @user · user/claude-workspace".
- Token: user access token + refresh token in the user's own DataOS secret (`dapp-github-<user>`), never shown or
  logged. **Disconnect** deletes it; the repo stays with the user.
- Claude Code:
  - on start, clone the repo into `~/work` (first time: push what is there);
  - git credential helper that fetches a fresh token on each push (no token on disk in plain text);
  - `CLAUDE.md` rule: commit and push after every task;
  - autosave every 15 minutes to an `autosave` branch;
  - `.gitignore` for `.env`, keys, `node_modules`, `.venv`, plus a secret scan that blocks a push with a token in it.
- Agent Builder:
  - each **Publish** commits `agents/<name>/vN.json` (configuration only, no tokens);
  - **Import from GitHub** lists agents in the repo and restores one.
- CLI (optional): `dapp github connect | status | disconnect`.
- Done when: a new Claude Code machine starts with the user's code, and a deleted agent comes back from GitHub.

### 3. Tools for agents: one place to connect MCP servers
Tools are connected once on the **Tools** page; agents only choose them.

- Agents tab: the Tools section lists connected servers as checkboxes, plus **+ Add tool**, which opens the Tools page
  with `?return=/agents/<id>`; **Back to agent** returns with the new tool already ticked.
- Tools page, **Add tool**:
  - presets: **Data Product MCP**, dapp-monitor, immortal-obj (DataOS identity, nothing to enter);
  - **Custom MCP**: enter only the URL; the dashboard detects how to connect:
    - OAuth: the server answers 401 with `WWW-Authenticate`; read `/.well-known/oauth-protected-resource` and the
      authorization server metadata; register with Dynamic Client Registration (or an admin-entered client ID);
      PKCE; **Sign in with …**;
    - key: an "API key / token" field and the header name (default `Authorization: Bearer`).
  - Result: "Connected · N tools" and the tool list.
- Per-user tokens in the user's DataOS secret (`dapp-mcp-<server>-<user>`); the gateway refreshes them and adds them on
  each call. Agents never see a token.
- Who acts: **as the user** (default: each person's own token, so DataOS and the tool's own permissions apply) or
  **as the owner** (opt-in, for shared tools like a team Notion). If a user hasn't connected a tool, the agent replies
  with a "Connect <tool>" link.
- Tools page row per server: auth type, status (connected / key expired), who may use it, which agents use it,
  usage, disconnect / rotate.
- Publish checks that every chosen tool is connected and allowed for the agent's app.
- First check `MAIN/dataos-mcp-oauth-broker`: it may already do the MCP OAuth part.
- Done when: a user adds Data Product MCP and one OAuth MCP (for example Notion) from the Agents tab, publishes an agent,
  and a second user gets the "Connect" link and then their own results.

### 4. Gateway settings from a file
- `deploy/gateway-config.yaml` lists tools (name, URL, secret for the token), allow lists and budgets; the gateway
  applies it on start. Tokens stay in DataOS secrets.
- Done when: a gateway with an empty database comes up with its tools and allow lists.

### 5. One-command restore
- `dapp restore` (or a script): Volumes on the current dataplane, apps in order (gateway, immortal-obj, dashboard,
  dapp-monitor, chatbot, log-assistant, claude-code), gateway config, keys rotated where the gateway lost them,
  backups restored, then an end-to-end check (real requests, not just HTTP 200: DataOS answers 200 with its own page).
- Runbook section "A dataplane is gone".

### 6. Platform asks (DataOS team)
- Why `poldp01` was removed without notice, and whether Volume disks are snapshotted at the cloud level.
- Open `files.pythonhosted.org` from `poliksdp01` (or give us a PyPI mirror). Until then, source apps with
  `requirements.txt` must use an image (see `apps/mcp-tool.Dockerfile`).

### 7. Finish open work
- Done: Agent Builder and the dashboard changes committed and live (dashboard 0.1.40: model picker with DataOS-hosted
  models, published agents in the chatbot's look). The gateway runs as a Service (`deploy/gateway.service.yaml`).
- Deploy the Log Assistant budget message fix (done with today's redeploy) and check it.
- Revoke the personal LLM key made during today's checks if not needed.

## Order and size

Next up (2026-11-27): task 3, Tools for agents.

| Task | Depends on | Size |
|---|---|---|
| 1 Backups | not doing | — |
| 7 Finish open work | — | done, except the two checks above |
| 4 Gateway config file | — | 1 day |
| 2 Connect GitHub | GitHub App from admin; 7 for the Agent Builder part | 3–4 days |
| 3 Tools for agents | 7 | 4–5 days |
| 5 Restore | 1, 4 | 1–2 days |
| 6 Platform asks | — | send today |

## Decisions needed
- ~~Where backups go~~: no backups; state stays on the immortal-obj Volume (2026-11-27).
- GitHub App (repo-level access, recommended) or OAuth App (simpler, all repos). Who creates it.
- Default for tools: "as the user" (recommended) and who may switch an agent to "as the owner".
