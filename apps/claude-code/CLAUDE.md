# Building on DataOS from this terminal

You run inside DataOS as the owner of this workspace (`$DATAOS_RUN_AS_USER`, tenant `$DATAOS_TENANT_ID`). Build what the user asks for,
deploy it with `dapp`, check it works, and give them the URL. Every `dapp` command acts as the user in DataOS, so only do what they asked.
There is no Docker here: apps are deployed from source (or from a ready image), models with `dapp model`.

## Quick recipes (use these first)

### "I want a low-cost chatbot" (a model AND a chatbot, about two minutes)
No code to write: the chatbot is a ready image, and the model runs on DataOS CPUs, so prompts stay in DataOS and there is no OpenAI bill.
1. Pick the model. `dapp model catalog` shows quality, speed (words/s), memory and an estimated cost per day. Say which you chose and its cost.
   Cheapest sensible: `qwen2.5-0.5b` (~$0.6/day, ~23 words/s). Better answers: `qwen2.5-1.5b` (~$1.2/day) or `llama3.2-3b` (~$1.4/day). Best: `qwen3-4b`.
   Costs are estimates from list prices; DataOS doesn't bill these apps. `dapp model deploy ID --dry-run` shows the numbers first.
2. `dapp model deploy qwen2.5-0.5b --name small` (the name is what the chatbot asks for; it waits until the model answers, 1-2 minutes,
   because it downloads on its first start).
3. `mkdir -p ~/work/lowcost-bot && cd ~/work/lowcost-bot && dapp init lowcost-bot --template chatbot --model small`
4. `dapp deploy --dry-run`, then `dapp deploy`, then wait for `dapp --json status lowcost-bot` to be running (30-90 s).
5. Check `curl -s -o /dev/null -w '%{http_code}' https://$DATAOS_FQDN/lowcost-bot/` gives 302, and tell the user the URL, the model and the cost,
   and how to share it (`dapp access allow lowcost-bot USER`).
The chatbot saves each person's chats (they survive restarts), shows every model deployed with `dapp model deploy` in its drop-down within seconds
(marked "starting…" until ready), and opens on one with `…/lowcost-bot/?model=NAME`. A system prompt goes in `env: SYSTEM_PROMPT:` in its dapp.yaml.
The user can also deploy and delete models in the dashboard's **Models** tab (a sortable table with the same numbers).

### "Chat with our data" (a chatbot over data products)
Small models cannot call tools, so the app does the tool work and the model only explains the numbers. Write a FastAPI app (see "Apps from source")
with `llm: true`, `objects: true`, `tools: [dataproduct-mcp]` (check `dapp tool ls`) and the model from `dapp model deploy`. Data Product MCP, through
the gateway: URL `$DAPP_TOOLS_URL/dataproduct-mcp`, headers `Authorization: Bearer $DAPP_LLM_KEY`, `x-dapp-user: <signed-in user>`,
`Accept: application/json, text/event-stream`; do `initialize`, `notifications/initialized`, then `tools/call` (replies are JSON or SSE; keep the
`mcp-session-id`; on 404 open a new session once). The tenant is the last part of `$DAPP_TOOLS_URL`. Tools, in order:
1. `search(index="data_product", tenant, q?, limit)` → text with lines `arn: arn:<tenant>/<dp_name>`;
2. `vulcan_semantic_schema(dp_name, tenant)` → JSON `{"schema": markdown}` with `## <model>` sections and `### Measures` / `### Dimensions` tables
   (`| Name | Type | Expression | Description | Instructions | Synonyms |`); the full name is `<model>.<name>`;
3. `vulcan_query_submit(dp_name, tenant, measures=[full names], dimensions=[…], limit=20)` → `{"statement_id"}`;
4. `vulcan_query_result(statement_id, dp_name, tenant)` every 3 s until `status` is `SUCCESS` (`results`, `row_count`); `FAILED` has the error.
Match the question to a measure (words before "by"/"per") and a dimension (after it) by name, description and synonyms; accept it only if it explains
at least 60% of the question's content words, else try the next data product (up to 3). Give the model at most ~5000 characters of rows. If no
number was computed, do not call the model: say so and list what can be asked (a 0.5B model invents numbers otherwise). Show each step in the chat
and say plainly when the MCP fails.

### "Move this data" / "bring table X into the Lakehouse" (DataOS Nilus pipelines)
1. `dapp pipeline depots` shows what each Depot can be moved from (batch, cdc, stream, metadata) and which can be written to (a Lakehouse).
2. Pick the answer to "how does the data change?" (`dapp pipeline kinds`): `once` (one copy), `refresh` (full copy on a schedule),
   `new_rows` (append, needs `--incremental-key`), `changes` (merge, needs `--incremental-key` and `--primary-key`), `live` / `live_history`
   (CDC, near real time; the source admin must enable change capture), `events` (Kafka), `catalog` (no rows, just the catalog).
   Prefer batch: it is cheaper and needs only read access. Use CDC only when the user needs changes within seconds.
3. `dapp pipeline new NAME --from DEPOT:schema.table --to DEPOT[:schema.table] --how ... [--schedule hourly|daily|"30 1 * * *"] --dry-run`,
   show the user the one-line summary, then run it without `--dry-run`. For a first try add `--limit 1000`.
4. `dapp --json pipeline status NAME` until the run is `completed` (about a minute for a small table); report rows and the destination table.
If it says the user can't use a Depot: nothing was created. **Never try to grant access yourself** (no grant manifests, no
`dataos-ctl` grants): tell the user to press Request access in the dashboard's Pipelines tab; the Depot's owner approves it there.
`dapp pipeline run NAME` runs a batch pipeline again; `dapp pipeline rm NAME --yes` only when asked (data it wrote stays).

### A custom app
Make one folder per app and deploy from source (next section), or start from a ready image with `dapp init NAME --template chatbot`.

## Apps from source (`runtime:`): no Docker

Make one folder per app, e.g. `~/work/sales-dash/`, with the code in `app/` and a `dapp.yaml` next to it:

```yaml
name: sales-dash                 # lower-case, digits, '-'; becomes the URL path /sales-dash
description: Sales dashboard
runtime: python-3.12             # pre-built image: no Dockerfile, no build
code: ./app                      # packed and shipped by dapp deploy (max ~700 KB; no data files)
run: streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --server.baseUrlPath sales-dash
compute: engineering-compute
resources: {cpu: 100m, memory: 256Mi, cpuLimit: "1", memoryLimit: 768Mi}
# private by default: DataOS sign-in in front of the app; only the owner until people are added
# access: {users: [alice, bob@corp.io]}
# public: true                   # only if the user explicitly wants anyone with the URL to open it
```

Rules for the code:
- Listen on `$PORT` on 0.0.0.0. The app is served under `/<name>/`, not `/`:
  - Streamlit: `--server.baseUrlPath <name>` (as above).
  - FastAPI: `app = FastAPI(root_path=os.environ.get("ROOT_PATH", ""))` and add `env: {ROOT_PATH: /<name>}` to
    dapp.yaml; run `uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers`. Use relative links in HTML.
- Already installed: fastapi, uvicorn, streamlit, pandas, numpy, plotly, httpx, requests, openai, anthropic,
  pydantic, jinja2. Anything else goes in `app/requirements.txt` (installed at start, keep it small).
- Python only (no Node, no system packages). Never put secrets in code or dapp.yaml.
- In a private app the signed-in user's DataOS id arrives in the `x-dapp-user` request header.

## LLMs: OpenAI models and models inside DataOS
- `llm: true` in dapp.yaml: the app gets `DAPP_LLM_KEY` and `OPENAI_BASE_URL` (the dapp gateway meters tokens per app and per person).
  Use `OpenAI(api_key=os.environ["DAPP_LLM_KEY"])`. OpenAI model: `gpt-6-luna` (with function tools pass `reasoning_effort` "none").
  A model deployed with `dapp model deploy` works the same way: `model="<its name>"` (no OpenAI key, no per-token bill).
- Models inside DataOS (small, CPU, private, prompts stay in DataOS):
  ```
  dapp model catalog                                   # id, quality, words/s, memory, estimated $/day (`--json` has more)
  dapp model deploy qwen2.5-1.5b --name my-model       # defaults fit the model; --dry-run shows settings + cost; waits until ready
  dapp model deploy --hf owner/repo:Q4_K_M --name x    # any GGUF repo on Hugging Face
  dapp model deploy ... --cpu-limit 4 --memory-limit 3Gi --set ctx=8192 --set temperature=0.3   # every Customize setting is a flag or --set
  dapp model ls                                        # ready or starting
  dapp model rm my-model --yes                         # only when the user asks
  ```
  They cannot call functions and have a 4096-8192 token context: give them plain text, do the tool work in the app.
  A model costs its reservation even when idle: delete what the user doesn't need. For a key to call one from a laptop:
  `dapp llm-key personal --save ~/.dapp/llm.env` (writes `DAPP_KEY` and `DAPP_BASE_URL` to a file; never print a key).
- `tools: [dataproduct-mcp]` (needs `llm: true`): the app may call that MCP tool at `$DAPP_TOOLS_URL/<tool>` with `Authorization: Bearer $DAPP_LLM_KEY`
  (MCP Streamable HTTP). See which tools exist with `dapp tool ls`.

## Storage that survives: immortal-obj
Apps lose their files on every restart. For anything an app must keep (chat history, settings, results), add
`objects: true` to dapp.yaml (it implies `llm: true`) and use the helper that is already installed:
```python
from dapp_objects import objects
db = objects.open("history")                          # this app's own object, created on first write
db.put("settings", {"theme": "dark"}); db.get("settings")
db.sql("create table if not exists msgs (who text, text text, at text default current_timestamp)")
db.sql("insert into msgs (who, text) values (?, ?)", [user, text])
rows = db.sql("select * from msgs where who = ? order by at desc limit 20", [user])   # list of dicts
mine = objects.open("chat", for_user=user)            # one object per signed-in person
```
Objects belong to the app (`app:<name>`); nobody else sees them unless the app shares them (`db.share(user)`, so the person can read it on the
dashboard's Storage page). Don't keep data in local files or SQLite on disk. The helper waits out a short gateway restart by itself.

The dashboard's **Apps** table shows, for every app, the secrets it uses (names only) and its Volumes (id and mount path, a disk, or
"none: files are lost on restart"). An app that must keep files and has none needs `objects: true` (above), not local files.

## Sharing and access
Apps are private (only the owner) until you add people: `dapp access allow NAME USER...`. Someone without access sees "No access" with a
**Request access** link; the owner sees the request on the app's Access tab in the dashboard and approves it there. Never make an app `public: true`
unless the user explicitly asks.

## Commands
```
dapp deploy -f dapp.yaml --dry-run     # check first
dapp deploy -f dapp.yaml               # create or update (a code change rolls the app in about a minute)
dapp --json status NAME                # wait until state is running
dapp logs NAME -n 100                  # the app's output, including [dapp-run] start-up lines
dapp access allow NAME USER...         # share a private app
dapp ls | dapp usage NAME | dapp tool ls | dapp model ls | dapp pipeline ls | dapp keys ls
dapp rm NAME --yes                     # only when the user asks
```

## Finish every task like this
1. Deploy, then poll `dapp --json status NAME` until it runs (start-up takes 30-90 s; a model takes 1-2 minutes more to download).
2. Check it: `curl -s -o /dev/null -w '%{http_code}' https://$DATAOS_FQDN/NAME/` gives 302 (sign-in page) for a
   private app or 200 for a public one; read `dapp logs NAME` if it crashes and fix the cause.
3. Tell the user the URL `https://$DATAOS_FQDN/NAME/`, who can open it, how to share it, and (for models) which one, its estimated cost per day, and how to delete it.
If a call fails with "connection refused" or 502 for about a minute, DataOS is moving the gateway: wait and retry, don't rebuild anything.

~/work is saved every 30 s to your immortal-obj volume `workspace` (and restored when this terminal restarts),
except caches, virtualenvs, .git, .env/keys, databases and files over 700 KB. Your Claude conversations are saved too
(`claude-sessions`). The Claude sign-in survives restarts when the app has a token secret (see apps/claude-code/dapp.yaml).
