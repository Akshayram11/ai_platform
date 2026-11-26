# dapp-monitor: watch apps and services (MCP tool, behind the gateway)

`dapp-monitor` is a read-only MCP server (`apps/dapp-monitor`) that lets an AI app, Claude Code or a person look at DataOS
resources: is it up, what are its pods, what did it just print, and what did it print at any time in the past, crashed and
replaced pods included. It is reached only through the dapp gateway, like `immortal-obj` and `dataproduct-mcp`:
`https://<host>/dapp-gateway/mcp/<tenant>/dapp-monitor`.

## Tools
Every tool takes `kind`: `app` (default), `service`, `worker`, `workflow` or `nilus`.

| Tool | What it answers | DataOS API |
|---|---|---|
| `list_resources(kind)` / `list_apps()` | what may be watched, with its state | Poros `GET /resources/<kind>?owner=` |
| `status(name, kind)` / `app_status(app)` | state, runtime, error, pods and their phase | Poros `GET /resources/<kind>/<name>` and `/runtime` |
| `recent_logs(name, kind, tail, pod)` | the running pods' last lines, now | Poros `GET /resources/<kind>/<name>/log?tailLines=&node=` |
| `search_logs(name, kind, query, since \| start+end, stream, pod, limit, offset)` | stored logs over **any time range**, old and crashed pods included, by keyword | platform-api `GET /platform-api/v2/tenants/<t>/resources/<kind>/<version>/<name>/logs/search` |
| `log_summary(name, kind, since \| start+end)` | lines, and how many match error / warn / exception / timeout, per pod | the same search, counted |

Time: `since="30m" | "1h" | "24h" | "7d"` (at most 31 days), or `start`/`end` as ISO UTC (`2026-11-26T16:00:00Z`; no zone means
UTC), epoch seconds, or `"2h ago"`. Results are oldest first, times in UTC, at most 1000 lines per call (`offset` pages).

What the model sees is cleaned up:
- **only the resource's own container** is searched (Poros runtime's `containers`, on its dataplane pods), not DataOS's log
  shipper sidecar (`<container>-0-sc`, vector) or the hub tunnel pod, whose warnings otherwise drown the app's lines
  (`include_sidecars=true` or `container=` to see them). Live, the chatbot's "errors" in a day went from 99 to 0 this way;
- **credentials are masked** in every line: `code=`, `state=`, `token=`, `apikey=`, `password=`… values, `Bearer …`, JWTs and
  `"password": "…"` become `•••`.

## Whose eyes, and who may call
- The pod runs as its owner (DataOS gives it `DATAOS_RUN_AS_USER` / `DATAOS_RUN_AS_APIKEY`), so it reads what the owner may read,
  no more.
- `MONITOR_ALLOW` (env) narrows it: `"*"` = everything the owner owns (any kind), or a list like `chatbot, service:orders-api`
  (a bare name is an app; another owner's resource can be listed if DataOS lets the owner read it).
- The gateway decides who may call the tool (`dapp tool allow dapp-monitor <user> app:<name>`) and records every call; it
  reaches the server with `TOOL_TOKEN` and `x-dapp-caller`. Without the token the server answers 401.

## From a terminal
`dapp logs NAME` is the running pod's tail; with a search it uses the same stored-log API as the tool:
```
dapp logs chatbot --search error --since 24h
dapp logs chatbot --search Traceback --from "2026-11-26T16:00:00Z" --to "2026-11-26T17:00:00Z" --stream stderr
dapp logs orders-api --kind service --since 1h
```
`dapp mcp` (the stdio MCP for Claude Code) has a `search_logs` tool too.

## Set up (done once in engineering, 2026-11-26)
```
python3 -c 'import secrets; print("TOOL_TOKEN=" + secrets.token_urlsafe(32))' > apps/dapp-monitor/.env      # gitignored
dapp secret set dapp-monitor token --from-env-file apps/dapp-monitor/.env --no-attach
dapp deploy -f apps/dapp-monitor/dapp.yaml
# the gateway may call it: deploy/gateway.dapp.yaml lists dapp-monitor in DAPP_TOOL_INTERNAL_HOSTS
(set -a; . apps/dapp-monitor/.env; set +a; dapp tool add dapp-monitor --url http://dapp-monitor:8080/mcp --auth-env TOOL_TOKEN \
     -d "watch DataOS apps: status, pods, recent logs, logs over any time range, error counts")
dapp tool allow dapp-monitor akshaychametmdcio            # and app:<chat-app> for the chat
```
An app uses it with `llm: true` and `tools: [dapp-monitor]` in its dapp.yaml, then MCP calls to `$DAPP_TOOLS_URL/dapp-monitor`
with `Authorization: Bearer $DAPP_LLM_KEY`.

`src/dapp/logsearch.py` holds the search (time parsing, checks, the call, masking); `apps/dapp-monitor/app/logsearch.py` is an
identical copy (a test keeps them equal) because app code ships alone. Tests: `tests/test_monitor.py`.

## The Log Assistant (demo app on top of it)
`examples/log-assistant` (live at `https://<host>/log-assistant/`): a page laid out like DataOS's resource Logs page (resource and
kind picker, Build/Runtime, pods, Live / Historical, keyword, 1m … 7d or a custom UTC range, the log table with WARN/ERROR
rows coloured), and an **AI** button. Pressing it opens a chat docked under the logs, on the same page.

- The page's data comes from `dapp-monitor` through the gateway (`/api/resources`, `/api/status`, `/api/logs`).
- The chat (`/api/chat`) sends the question to `gpt-6-luna` through the gateway (metered per person, `x-dapp-user`) with only the
  monitor's functions as tools and `reasoning_effort: none` (chat completions refuses function tools with reasoning on). The
  system message says what the page shows (resource, kind, Live/Historical, keyword, window, pod) and the time now, so
  "is anything wrong here?" means this resource and this window. Up to 8 tool rounds per question.
- Each answer shows what was looked at as chips (`log_summary → 75 lines: 0 error …`, `search_logs → 75 matching lines,
  showing 5`) and quotes the log lines it used. It answers in the user's language (Hinglish in, Hinglish out).
- Deploy: `dapp deploy -f examples/log-assistant/dapp.yaml` (`llm: true`, `tools: [dapp-monitor]`; the deploy also allows
  `app:log-assistant` on the tool). Private: only the owner until people are added, and everyone who can open it sees what
  dapp-monitor may show (`MONITOR_ALLOW`). Tests: `tests/test_log_assistant.py`.

## Not yet
- An audit trail (who deployed or deleted what, when): no single DataOS API found yet; cloud events search needs index names.
- Metrics (CPU, memory over time) as a tool.
