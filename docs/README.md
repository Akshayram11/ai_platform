# Docs

What we built on top of DataOS to make it an app cloud, and how to run it. The top-level [README](../README.md) is the
quick start; these pages go deeper.

| Page | Read it for |
|---|---|
| [architecture.md](architecture.md) | The pieces and how a request moves between them |
| [llm-gateway.md](llm-gateway.md) | App and personal LLM keys, usage metering, the in-DataOS model `small` |
| [immortal-obj.md](immortal-obj.md) | Durable per-person and per-app storage (the Durable Objects equivalent) |
| [run-from-source.md](run-from-source.md) | Deploying Python apps with no Docker image (`runtime:` + `code:`) |
| [claude-code.md](claude-code.md) | Claude Code in the browser: what it can do, staying signed in, resources |
| [data-chat.md](data-chat.md) | Chat over data products: small model + Data Product MCP, how a question is answered |
| [dashboard.md](dashboard.md) | The dashboard: apps, Storage, access requests, usage |
| [runbook.md](runbook.md) | Versions that are live, build and deploy commands, gotchas we hit |
| [MANAGER_PROMPT.md](MANAGER_PROMPT.md) | One prompt to have an agent deploy a chatbot |

Instance: `https://poliks.instance.dataos.cloud`, tenant `engineering`, compute `engineering-compute`, dataplane `poldp01`.

Rule for every page: no secret values anywhere (tokens, keys, `.env` contents). Docs name the secret, never its value.
