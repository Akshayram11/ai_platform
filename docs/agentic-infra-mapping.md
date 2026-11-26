# Agentic infrastructure: where we are

Maps the product story "DataOS: agentic infrastructure for a business that builds with AI" (draft, 23 Nov 2026) onto what
this repo does today. Honest status: **Built** works on the live instance, **Partial** is metered or seen but not enforced,
**Gap** does not exist yet.

The story: business people build apps and agents with AI; engineering sets standards once through the System Control
Protocol (SCP); every resource is an **Entity**, **Data** or **Governance** resource with a lifecycle, owner and traces.
What we built is a working prototype of the first step, "bring one app or agent under DataOS".

## Entities

| Story | We have | Status |
|---|---|---|
| Apps and agents | `dapp deploy` from a small `dapp.yaml`; Python apps need no image ([run-from-source.md](run-from-source.md)); Claude Code in the browser deploys as the signed-in user ([claude-code.md](claude-code.md)) | Built |
| Connections and tools | `dapp tool add`: MCP servers behind the gateway, credentials stored once, per-app allow lists ([llm-gateway.md](llm-gateway.md)) | Built |
| Models | Gateway to providers, plus the in-DataOS model `small` (llama.cpp, Volume `llm-models`) | Built |
| Sandbox for agent code | The Claude Code pod is a container, not a sandbox: it runs with the user's own DataOS key | Gap |

## Data

| Story | We have | Status |
|---|---|---|
| Agent memory | [immortal-obj](immortal-obj.md): per-person and per-app durable storage; the chatbot keeps chat history in it | Built |
| Datasets, definitions, ontologies | Read only, through the Data Product MCP; we create none | Partial |
| System prompts, documents | Live in each app's code or object storage; no shared, versioned registry | Gap |

## Governance

| Story | We have | Status |
|---|---|---|
| Identity | DataOS sign-in, an owner per app, one gateway key per app, personal keys per person (LLM only) | Built |
| Credentials | Provider keys and tool credentials exist only in the gateway; apps never hold them | Built |
| Policies | Private by default, `access` lists, tool allow lists | Partial |
| Budgets | Tokens and cost are counted per tenant, app, owner, model and end user (`dapp usage`, dashboard). Nothing is capped | Partial |
| Traces | Usage and tool-call records, CPU and memory per app. No agent step traces | Partial |
| Checks and approvals | Dashboard has access requests. No pre-deploy standards check, no promotion approvals | Gap |

## Lifecycle

| Story | We have | Status |
|---|---|---|
| Owner, handover, retire | Owner is recorded; `dapp rm` retires. No transfer command, no stale-app sweep | Partial |
| Promotion ladder (personal, team, org) | None | Gap |
| Change checked against dependents | None; the gateway knows which app uses which tool or model, so the data exists | Gap |
| Reconciliation | Poros reconciles the pods; nothing reconciles owner, dependencies or policy | Gap |
| Apps outside DataOS call through it | Laptop with a personal key or `dapp tool connect` works | Partial |

## Feasible on the platform vs. built by us

- **DataOS gave us:** apps, Volumes, secrets, compute, ingress, sign-in and permissions (Heimdall).
- **We built because it was missing:** LLM keys and token metering, app CPU and memory reporting, a tool gateway,
  durable per-person storage.
- **Platform limits we hit:** inline `disk:` apps break on update (use a Volume); no way to ask Heimdall "can this user use
  this resource", so immortal-obj keeps its own share table.

## What is needed

**We can build (small):**
1. Budget caps in the gateway, per app and per user (counting already exists).
2. Pre-deploy checks in `dapp deploy`: no public without approval, resource limits required, allowed tools only.
3. A dependency and staleness view: which app uses which tool, model or object; which have not run in 30 days.
4. `dapp transfer` and a retire sweep for ownerless or unused apps.

**Needs the platform team:**
5. A limited identity per agent. Today the Claude Code pod acts with its builder's full access, which is the risk the
   story names. Fix: one identity per agent with narrow permissions, plus network limits.
6. Heimdall-backed sharing for immortal-obj instead of its own table.
7. Promotion ladder and approvals (the SCP and resource-model part).
8. A decision on traces: store agent steps or not. We keep numbers only on purpose (privacy).

**Prototype to production:**
9. immortal-obj is SQLite on one Volume with a single writer: no HA, backup or quota.
10. Only the Python runtime exists; no Node.
