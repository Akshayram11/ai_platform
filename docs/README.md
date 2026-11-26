# Docs

What we built on top of DataOS to make it an app cloud, and how to run it. The top-level [README](../README.md) is the
quick start; these pages go deeper.

| Page | Read it for |
|---|---|
| [architecture.md](architecture.md) | The pieces and how a request moves between them |
| [pipelines.md](pipelines.md) | The Pipelines tab and `dapp pipeline`: move data with Nilus in plain words, governed Depot access |
| [nilus-custom-sources.md](nilus-custom-sources.md) | Custom API sources with Nilus (parked): how they work, why Git, where the code could live, the plan |
| [nilus-notes.md](nilus-notes.md) | What we tested live about Nilus: permissions, purposes, runs API, re-run behaviour |
| [models.md](models.md) | The Models tab: deploy a small model in a few clicks, every setting in Customize, use and delete it |
| [llm-gateway.md](llm-gateway.md) | App and personal LLM keys, usage metering, the in-DataOS model `small` |
| [immortal-obj.md](immortal-obj.md) | Durable per-person and per-app storage (the Durable Objects equivalent) |
| [run-from-source.md](run-from-source.md) | Deploying Python apps with no Docker image (`runtime:` + `code:`) |
| [claude-code.md](claude-code.md) | Claude Code in the browser: what it can do, [dapp MCP setup](claude-code.md#add-the-dapp-mcp-to-online-claude-code), staying signed in, resources |
| [data-chat.md](data-chat.md) | Chat over data products: small model + Data Product MCP, how a question is answered |
| [dashboard.md](dashboard.md) | The dashboard: apps, Storage, access requests, usage |
| [agentic-infra-mapping.md](agentic-infra-mapping.md) | How this maps to the agentic-infrastructure story: built, partial, gaps, what is needed |
| [runbook.md](runbook.md) | Versions that are live, build and deploy commands, gotchas we hit |
| [MANAGER_PROMPT.md](MANAGER_PROMPT.md) | One prompt to have an agent deploy a chatbot |

Instance: `https://poliks.instance.dataos.cloud`, tenant `engineering`, compute `engineering-compute`, dataplane `poldp01`.

Rule for every page: no secret values anywhere (tokens, keys, `.env` contents). Docs name the secret, never its value.
