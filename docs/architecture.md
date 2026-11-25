# Architecture

```
 laptop                       DataOS (poliks, tenant engineering)
 ──────                       ───────────────────────────────────────────────────────────
 dapp CLI ───────────────────▶ Poros (apps, secrets, volumes)      Heimdall (who is who)
                                   │
  browser ──▶ ingress (Kong) ──▶ apps ───────────────┐
                                   │                  ▼
   dashboard  /dapp          ┌─────┴──────┐   dapp gateway  /dapp-gateway
   chatbot    /chatbot       │ your apps  │──▶  • app + personal LLM keys, metering
   claude-code /claude-code  └─────┬──────┘     • OpenAI, and local model "small" ──▶ llm-small
   your apps  /<name>              │            • MCP tools: demo-tools, immortal-obj, (dataproduct-mcp)
                                   ▼
                              immortal-obj  ◀── only reachable through the gateway
                              (Volume immortal-obj: one SQLite file per object)
```

## The pieces

| Piece | What it is | Where |
|---|---|---|
| `dapp` CLI | Deploy and manage apps from one small `dapp.yaml`; talks to DataOS as you | `src/dapp/` |
| Dashboard | Web UI: apps, access, secrets, logs, LLM usage, Storage, tools, keys | `src/dapp_dashboard/`, `deploy/dashboard.dapp.yaml` |
| LLM gateway | Per-app keys, token metering, provider keys kept in one place, MCP tool proxy, local models | `src/dapp_gateway/`, `deploy/gateway.dapp.yaml` |
| llm-small | Qwen2.5-0.5B on CPU (llama.cpp), reached as model `small` | `apps/llm-small/` |
| immortal-obj | Durable objects (key-value, SQL, files) per person and per app | `apps/immortal-obj/` |
| claude-code | Claude Code in a browser terminal with `dapp` inside | `apps/claude-code/` |
| Runtime image | Python 3.12 image that runs an app from source (no Docker build) | `runtimes/` |
| Chatbot example | Chat UI: model switch, MCP tools, history in immortal-obj | `examples/chatbot/` |

## Who is trusted for what
- **People** sign in with DataOS (OIDC); every dashboard call runs *as them*, so DataOS permissions still apply.
- **Apps** prove themselves to the gateway with their own `dapp_...` key (created by `llm: true`).
- **The gateway** tells tools who is calling in `x-dapp-caller` (a user id or `app:<name>`); clients cannot set it.
  Tools such as immortal-obj accept calls only with the gateway's token, so nobody can claim to be someone else.
- **Secrets** live in DataOS secrets. A secret is named in docs, never shown.

## What DataOS gives and what dapp adds
DataOS gives: Poros (run containers), Heimdall (identity), secrets, Volumes, compute, ingress.
dapp adds: the one-file deploy, private-by-default apps with an allow list, LLM keys and metering, an in-DataOS
model, tool governance, durable per-person storage, self-reported CPU/memory (DataOS does not collect pod metrics for
app namespaces), the dashboard, and Claude Code that deploys for you.
