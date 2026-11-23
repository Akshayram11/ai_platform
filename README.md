# ai_platform

Use DataOS as an app platform (the Python package is `dataos-apps`, the CLI is `dapp`). People and AI agents (Claude Code, Codex, …) deploy any container
with one small file, and everyone can see what they've deployed, what it uses, and who can reach it.

| Piece | What it does |
|---|---|
| **`dapp` CLI** (`src/dapp`) | Deploy, list, status, logs, scale, delete apps. Also manages secrets, access grants, DataOS API keys and usage. Every command takes `--json`, and `dapp mcp` serves the same operations to agents. |
| **LLM gateway** (`src/dapp_gateway`) | Apps call it instead of OpenAI/Anthropic. It holds the real provider keys, gives each app its own key, and records tokens per app, owner and model. |
| **Dashboard** (`src/dapp_dashboard`) | Shows your apps with status, pods, CPU/memory, LLM tokens and cost, secrets, access, logs and keys. Every call runs as the signed-in user, so DataOS permissions apply. |

## Install on a laptop (and remove)

You need `git`, `dataos-ctl` (logged in: `dataos-ctl login`, then `dataos-ctl tenant select`) and access to this repo.
Nothing else: the installer brings `uv` and a private Python for `dapp`, and does not touch your system Python.

```bash
git clone git@github.com:Akshayram11/ai_platform.git
./ai_platform/install.sh          # installs dapp, then runs `dapp doctor`
dapp doctor                       # is this machine ready to deploy?
dapp init mychat --template chatbot && dapp deploy      # a ready chatbot, nothing to build
```

`install.sh` does not edit your shell files, log you in, or deploy anything. It prints the one `PATH` line to add if
needed. Options: `--ref <branch|tag>`, `--mcp` (also register `dapp` with Claude Code), `--no-doctor`.
Run it again to update (`git pull && ./install.sh`).

```bash
./ai_platform/uninstall.sh        # asks first; --yes for scripts and agents
```

`uninstall.sh` removes the `dapp` tool, the clone made by the installer, `~/.dapp/config.yaml` and the Claude Code
entry. It keeps your `dataos-ctl` login, your own clone, and **everything you deployed** (delete apps first with
`dapp rm <app> --purge`). No `dataos-ctl`? `dapp setup` saves a DataOS API key instead (Keys page in the dashboard).
`dapp doctor` explains anything that is not ready, and tells you when your 24-hour login expired.

To hand the whole thing to an agent, use the prompt in [docs/MANAGER_PROMPT.md](docs/MANAGER_PROMPT.md).

## How it fits DataOS

```
dapp.yaml ──dapp deploy──► dataos-ctl resource apply --in   (or Poros REST)
                              │
                              ▼
                    app resource (v1alpha)  ──Poros──►  service <name>svc
                                                        Deployment + Service + Kong ingress (/<path>) + HPA
                                                        secrets → env vars, runs on a compute
reads:   Poros  /poros/api/v2/tenants/{t}/resources[...]/runtime|log
         Heimdall /heimdall/api/v1/{authorize,users/{id}/tokens,tokens}
usage:   Thanos/Prometheus (container_cpu_usage_seconds_total, …)   +   dapp gateway (/admin/usage)
access:  grant resources (Heimdall policy use cases), tagged dapp-app-<name>
```

* Writes go through **dataos-ctl** when it's on PATH and pointed at the same context, because it lints, resolves stacks and sets owners. Manifests go in over stdin, so secret values never touch disk. Otherwise dapp writes straight to the Poros API (`DAPP_WRITER=api|ctl` to force one).
* dapp reuses the **dataos-ctl login** (`~/.dataos/v2`). Headless agents and pods use `DATAOS_FQDN`, `DATAOS_TENANT_ID`, `DATAOS_RUN_AS_USER` and `DATAOS_RUN_AS_APIKEY`.

## Quick start

```bash
uv sync --all-extras          # or: pip install -e '.[all]'
uv run dapp context           # which instance / tenant / user dapp will use
uv run dapp init hello        # writes dapp.yaml
uv run dapp deploy --dry-run  # shows the DataOS app manifest
uv run dapp deploy
uv run dapp ls
uv run dapp status hello
uv run dapp logs hello -n 50
```

### dapp.yaml

```yaml
name: chatbot
description: OpenAI chatbot
image: registry/dapp-chatbot:0.1.6
port: 8080
compute: runnable-default
env: {OPENAI_MODEL: gpt-4o-mini}
secrets: []          # existing DataOS secrets → env vars
llm: true            # route OpenAI/Anthropic via the gateway, meter tokens
public: false        # true = no DataOS login in front of the app
# runAs, replicas, resources, autoscale, path, workingDirs, imagePullSecret, hostname, owners, tags
```

### Commands

```
dapp deploy [-f dapp.yaml] [--dry-run] [--lint] [--rotate-llm-key]
dapp ls [--all]          dapp status APP          dapp logs APP [-n 100]
dapp scale APP N         dapp rm APP [--purge] --yes
dapp secret ls APP       dapp secret set APP NAME K=V… [--from-env-file F] [--no-attach]
dapp secret rm APP NAME
dapp access ls APP       dapp access grant APP USER --use-case ID [--value ATOM:KEY=VAL]
dapp access revoke GRANT
dapp keys ls|create NAME|rm NAME [--force]     # DataOS API keys (names: letters, digits, _)
dapp llm-key ls | rotate APP | revoke APP PREFIX   # per-app LLM gateway keys
dapp usage [APP]
dapp mcp                 # MCP server over stdio
```

### Agents

```bash
claude mcp add dapp -- uv --directory /path/to/dataos-apps run dapp mcp
```

Tools: `whoami, list_apps, plan_app, deploy_app, app_status, app_logs, scale_app, delete_app,
list_secrets, set_secret, delete_secret, list_access, grant_access, revoke_access, usage`.
Destructive tools need `confirm=true`. Agents can also run `dapp … --json`. Nothing prompts or opens a browser.

## Example: OpenAI chatbot (`examples/chatbot`)

### Where the OpenAI key goes

**Never commit it, and never paste it into chat.** Pick one option:

* **Through the gateway (recommended).** The key lives only in the gateway's DataOS secret. Apps get a dapp key, and token usage shows in the dashboard.
  ```bash
  cp examples/chatbot/.env.example .env.gateway       # put OPENAI_API_KEY=... in it (gitignored)
  dapp secret apply deploy/secrets/dapp-gateway-providers.yaml --env-file .env.gateway
  dapp deploy -f deploy/gateway.dapp.yaml
  dapp deploy -f examples/chatbot/dapp.yaml           # llm: true
  ```
* **Directly.** The app holds the key. Set `llm: false` and uncomment `secrets:` in the chatbot's dapp.yaml.
  ```bash
  cp examples/chatbot/.env.example examples/chatbot/.env   # put OPENAI_API_KEY=... in it
  dapp secret apply deploy/secrets/chatbot-openai.direct.yaml --env-file examples/chatbot/.env
  dapp deploy -f examples/chatbot/dapp.yaml
  ```

To run it locally first, set the key in your shell and run `uvicorn app:app --port 8080`
from `examples/chatbot` (`pip install -r requirements.txt`).

## Building another LLM app (not a chat)

`examples/llm-api` is a JSON API that summarizes documents; `deploy/grants/app-developer.template.yaml` is what a
developer needs besides logging in (create resources, use the compute, read their `dapp-*` secrets). With
`llm: true` the app uses the standard SDK and passes one key: `OpenAI(api_key=os.environ["DAPP_LLM_KEY"])` (or
`Anthropic(api_key=...)`). dapp sets `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL` and `DAPP_LLM_GATEWAY_URL`, and the
app's `dapp-<app>-llm` secret holds only `DAPP_LLM_KEY`. The gateway holds the real provider keys and meters tokens
and cost per app.

## Secrets as YAML (`deploy/secrets/`)

| File | Who creates it | Holds |
|---|---|---|
| `dapp-gateway-providers.yaml` | you, with `dapp secret apply ... --env-file .env.gateway` | the real provider keys (`OPENAI_API_KEY`, optionally `ANTHROPIC_API_KEY`, `DATABASE_URL`) |
| `app-llm.generated.yaml` | `dapp deploy` (shape only) | `DAPP_LLM_KEY`, the app's own gateway key |
| `app-oidc.generated.yaml` | `dapp deploy` (shape only) | `clientSecret`, `DAPP_SESSION_KEY` for the app's sign-in |
| `chatbot-openai.direct.yaml` | you, only without the gateway | `OPENAI_API_KEY` for `llm: false` |

No file contains a key: values are `${VAR}` references or `<placeholders>`, and `dapp secret apply` refuses unset
variables, placeholders and non-secret resources, and prints key names only. A test keeps the shapes in sync with
what `dapp deploy` really creates.

## Never toggle `stateful` / `disk` on a live app

Poros builds either a StatefulSet or a Deployment from the app and does **not** delete the other kind. Switching an
existing app (we did it with the gateway) leaves the old workload running as an orphan that DataOS no longer lists but
that the Service still selects: intermittent 401s and usage split across two databases. A workload is named from the
app's creation stamp, so the fix without cluster access is to delete the app and create it again (new stamp, new
selector), ideally under a new name so the orphan's metrics don't count toward the new app. Decide `disk:` before the
first deploy. The gateway is now `dapp-llm-gateway` (hostname `dapp-gateway` and path `/dapp-gateway` unchanged).

## CPU and memory

DataOS does not collect pod CPU/memory for app namespaces (poliks' Thanos has no `container_*` or `kube_pod_*`
series), so apps report their own. Set `metrics: true` in `dapp.yaml` and run the small helper in the app:

```python
import dapp_metrics
dapp_metrics.start()                  # /metrics on port 9102, reads the container's cgroup
app = dapp_metrics.instrument(app)    # optional: request counts and latency for an ASGI app
```

`metrics: true` sets `metricEndpoint: {port: 9102, path: /metrics}`. DataOS then runs a sidecar that scrapes
`localhost:9102/metrics` inside the pod (never through the ingress or the sign-in) and remote-writes it to
Prometheus with `tenant` and `job_name=<app name>`. The dashboard and `dapp usage APP` query
`https://<fqdn>/observability` as the signed-in user (override with `DAPP_PROMETHEUS_URL`). Apps without the
helper show "no data". Series are prefixed `dapp_` and carry a `pod` label so replicas add up.

## Running the platform pieces

```bash
docker build -t <registry>/dataos-apps:0.1.12 .
# fill the REPLACE_ME values, then:
dapp deploy -f deploy/gateway.dapp.yaml
dapp deploy -f deploy/dashboard.dapp.yaml     # → https://<fqdn>/dapp/
```

Local dashboard against your dataos-ctl login:

```bash
DAPP_DEV=1 uv run dapp-dashboard               # http://localhost:8080
```

## Open points (need a decision or a live cluster to confirm)

1. **App access control.** `dapp access grant` creates a Heimdall `grant` for any policy use case. It's still open which use case should mean "may open this app's URL". Either an existing per-path use case, or a new `view-dapp-app` use case with a path value.
2. **CPU/memory only for apps that run `dapp_metrics`** (see above); an arbitrary container shows no data.
3. **Dashboard sign-in** uses an API key today. Browser OIDC through Heimdall (as databots does) is the next step.
4. **Gateway storage** defaults to SQLite on an emptyDir. Set `DATABASE_URL` to Postgres before relying on the numbers.
5. **Pricing** is off until you set `DAPP_PRICING`. No prices are built in, because they change.
