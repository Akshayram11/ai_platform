# LLM gateway

One service (`dapp-llm-gateway`, `/dapp-gateway`) in front of every model call and every MCP tool call.

## Keys
- **App key.** `llm: true` in `dapp.yaml` makes `dapp deploy` ask the gateway for a key for that app and store it in
  the secret `dapp-<app>-llm` (`DAPP_LLM_KEY`). The app also gets `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`. The gateway keeps
  only a hash. The real provider key lives only in the gateway's secret `dapp-gateway-providers`.
- **Personal key** (for your laptop or any OpenAI-compatible tool): `dapp llm-key personal --save ~/.dapp/llm.env`
  writes `DAPP_KEY` and `DAPP_BASE_URL` to a mode-600 file and never prints the key. Usage is counted as app
  `user:<you>`. `--rotate` revokes your earlier personal keys. It works for LLM calls only; MCP tools refuse it.
  Only you can create, rotate or revoke your personal key.
- Rotate or revoke an app key: `dapp llm-key rotate APP`, `dapp llm-key revoke APP PREFIX`. `dapp llm-key ls` shows prefixes.

```bash
set -a; . ~/.dapp/llm.env; set +a
curl -s "$DAPP_BASE_URL/chat/completions" -H "Authorization: Bearer $DAPP_KEY" \
  -H "Content-Type: application/json" -d '{"model":"small","messages":[{"role":"user","content":"hi"}]}'
```
Base URL for other tools: `https://poliks.instance.dataos.cloud/dapp-gateway/openai/v1`, model `small`, key = your personal key.

## Usage
Every call records tenant, app, owner, model, end user, tokens (input, output, cache) and errors, never the text.
- `dapp usage APP [--by end_user]`, dashboard app page → "LLM usage" tab.
- `GET /admin/usage?app=&model=&group_by=app,model,day,end_user`.
- An app tells the gateway who is using it with the `x-dapp-user` header (the dashboard and chatbot do this).
- `small` has no price, so its cost shows `–`, not `$0`.

## Local model: `small` (removed; now a dashboard model)
This section describes how `small` was first deployed by hand (`apps/llm-small`, still in the repo as an example). It was removed on 2026-11-25; the same model comes back from the
**Models** tab (Qwen2.5 0.5B, name it `small`), see [models.md](models.md). The gateway can still serve a fixed model through `DAPP_LOCAL_MODELS`.

Qwen2.5-0.5B-Instruct (Q4_K_M, ~470 MB) served by llama.cpp as the DataOS app `llm-small`. Prompts never leave DataOS.
- The gateway reads `DAPP_LOCAL_MODELS='{"small": {"url": "http://llm-small:8080", "key_env": "LLM_SMALL_KEY"}}'`. A request
  for model `small` on `/openai/v1/chat/completions` goes to llm-small (provider `local`) instead of OpenAI, with the
  same keys, metering and budgets. `/healthz` shows `local_models` and which app serves each.
- llm-small is **private**: DataOS' ingress refuses everyone from outside (401, and 403 even with an owner's DataOS
  key), and llama-server itself needs `LLAMA_API_KEY`, which only the gateway has (secret `dapp-gateway-local`, key
  `LLM_SMALL_KEY`). The gateway reaches it inside the cluster at `http://llm-small:8080`.
- Speed on 2 CPU threads: ~28 tokens/s, first token well under a second. Context 4096, one request at a time.
  It cannot do tool/function calling; give it plain text (the chatbot only sends tools to the main model).
- The model file lives on the Volume `llm-models` (`/models`). A restart loads it in ~3 s with no download; the pod log
  prints `[llm-small] model cache /models: …`.

Set up from scratch (see also the comments in `apps/llm-small/dapp.yaml`):
```bash
dataos-ctl resource apply -f apps/llm-small/models.volume.yaml
python3 -c 'import secrets; print("LLAMA_API_KEY=" + secrets.token_urlsafe(32))' > apps/llm-small/.env   # gitignored
dapp secret set llm-small key --from-env-file apps/llm-small/.env --no-attach
dapp deploy -f apps/llm-small/dapp.yaml
# gateway: same key as LLM_SMALL_KEY in secret dapp-gateway-local, DAPP_LOCAL_MODELS in deploy/gateway.dapp.yaml
```
Numeric settings go in `LLAMA_ARG_*` env vars, because DataOS rejects numeric-looking `args`.

## Models people deploy
The dashboard's **Models** tab deploys more models like `small` ([models.md](models.md)). The gateway keeps them in a `local_models` table (name, owner, app, url, the
server's key encrypted) and routes `model: <name>` from keys of the same tenant; `GET /admin/models` lists them and the config ones, each with `ready` (the server's `/health`); `GET /local-models` does the same for an app's own key (the chatbot's drop-down).

## MCP tools through the gateway
`dapp tool add NAME --url … --auth-env VAR`, `dapp tool allow NAME "*" | user | app:name`, `dapp tool usage`. The gateway checks the
allow list, injects the tool's credential (encrypted at rest), and records who called which function. Registered now:
`demo-tools`, `immortal-obj`. `dataproduct-mcp` must be registered by the person who has the API key (enter it with
`read -s`, never in a file that is committed). An app that lists `tools: [name]` in `dapp.yaml` is allowed on it
automatically when its owner owns the tool.

## Gateway storage
The gateway keeps keys, usage and tools in SQLite on the Volume `dapp-gateway-data` (`/data`). Use a Volume, not an
inline `disk:`: see [runbook.md](runbook.md).
