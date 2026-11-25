# Runbook

## Live now (poliks, as of 2026-11-25)
| App | Image | Notes |
|---|---|---|
| `dapp` (dashboard) | `dataos-apps:0.1.23` | `objects: true` |
| `dapp-llm-gateway` | `dataos-apps:0.1.21` | Volume `dapp-gateway-data`; local model `small`; personal keys |
| `llm-small` | `llm-small:b12679-m1` | Volume `llm-models`; private; metrics on |
| `immortal-obj` | `dapp-runtime:python-3.12-r7` | Volume `immortal-obj` |
| `chatbot` | `dapp-chatbot:0.1.12` | `llm`, `objects`, `metrics`; models `gpt-6-luna` and `small`; history |
| `data-chat` | runtime `python-3.12-r8` from source | `llm`, `objects`, `tools: [dataproduct-mcp]`; model `small` |
| `claude-code` | `dapp-claude-code:0.1.4` | 500m→4 CPU, 2→4 GiB; token secret `dapp-claude-code-anthropic` |
| Runtime image | `dapp-runtime:python-3.12-r8` | what `RUNTIME_REVISION` points at |
All images are under `docker.io/tmdcio/`. Nothing of this work is committed to git yet.

## Build and deploy
```bash
uv tool install --force .                         # refresh the global `dapp` from the repo (it lags otherwise)
uv run pytest -q && uv run ruff check src tests examples apps
docker buildx build --platform linux/amd64 -t docker.io/tmdcio/dataos-apps:0.1.N --push .      # dashboard + gateway
docker buildx build --platform linux/amd64 -f examples/chatbot/Dockerfile -t docker.io/tmdcio/dapp-chatbot:0.1.N --push .
docker buildx build --platform linux/amd64 -f apps/llm-small/Dockerfile -t docker.io/tmdcio/llm-small:b12679-mN --push .
dapp deploy -f deploy/gateway.dapp.yaml           # then deploy/dashboard.dapp.yaml, examples/chatbot/dapp.yaml, ...
dapp --json status NAME                           # wait for running; a new pod name means the rollout finished
dapp logs NAME -n 100
```
Docker Desktop must be running to build. Bump the image tag in the manifest (and `CHATBOT_IMAGE`, `CLAUDE_CODE_IMAGE` in `src/dapp/cli.py`).
After a deploy that replaces a pod, check the new pod's logs rather than the old one's.

## Secrets (names only)
`dapp-gateway-providers` (OpenAI key), `dapp-gateway-tools` (tool credentials), `dapp-gateway-local` (`LLM_SMALL_KEY`),
`dapp-llm-small-key` (`LLAMA_API_KEY`), `dapp-immortal-obj-token` (`TOOL_TOKEN`), `dapp-claude-code-anthropic`
(`CLAUDE_CODE_OAUTH_TOKEN`), `dapp-<app>-llm` per app, `dapp-<app>-code` per source app, `dapp-<app>-oidc` per private app.
Local `.env` files (gitignored): `apps/immortal-obj/.env`, `apps/llm-small/.env`, `.env.gateway-local`, `.env.gateway-tools`,
`examples/mcp-tool/.env`. Never print, log or commit them. A person enters their own credentials (`read -s`).

## Gotchas we hit
| Symptom | Cause and fix |
|---|---|
| App with `disk:` goes `error(qt)` on update | Inline disk apps cannot be updated on Poros. Use a standalone **Volume** (`volumes:`); updates in place and keeps data. DataOS requires such an app to be stateful and single pod |
| "volume cannot be attached to a stateless service" | `volumes:` makes dapp set `stateful: true` (and forbids replicas > 1, `disk`, autoscale) |
| `Forbidden` deploying to a compute | No Can Use on that compute (`runnable-default`); use `engineering-compute` |
| "expected string, but got number" | DataOS rejects numeric `args`; put numbers in env (`LLAMA_ARG_*`) |
| Cloudflare 1010 from a Python client | The edge blocks urllib's default agent; `dapp_objects` sends its own `User-Agent` |
| Global `dapp` rejects `volumes:` / `objects:` | It is older than the repo: `uv tool install --force .` |
| `dapp tool add` has no `--env-file` | `(set -a; . file; set +a; dapp tool add … --auth-env VAR)` |
| Compute card shows pods of other people | Metrics are filtered by service name now ([dashboard.md](dashboard.md)) |
| Claude asks to sign in again | Pod replaced; use the token secret ([claude-code.md](claude-code.md)) |
| llm-small: every call from outside is 401/403 | Intended (private); go through the gateway |
| `sed -i` on macOS | needs `sed -i ''` |

Never toggle `stateful` or `disk` on a live app: delete and recreate with the user's agreement, because data does not move.

## Tests
`uv run pytest -q` (108 at the last run) covers the CLI, gateway, tools, immortal-obj (store, SQL guard, server with the helper,
Claude workspace), dashboard pages (including Storage and access requests against a real immortal-obj server), login, chatbot
(model switch, history). `uv run ruff check src tests examples apps` must be clean.
