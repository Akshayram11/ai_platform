# Building apps on DataOS from this terminal

You run inside DataOS as the owner of this workspace (`$DATAOS_RUN_AS_USER`, tenant `$DATAOS_TENANT_ID`). Build what
the user asks for, deploy it with `dapp`, check it works, and give them the URL. Every `dapp` command acts as the
user in DataOS, so only do what they asked.

## There is no Docker here: deploy from source

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
- Python only for now (no Node, no system packages). Never put secrets in code or dapp.yaml.
- In a private app the signed-in user's DataOS id arrives in the `x-dapp-user` request header.

## LLM and data
- `llm: true` in dapp.yaml: the app gets `DAPP_LLM_KEY` and `OPENAI_BASE_URL` (the dapp gateway meters tokens).
  Use `OpenAI(api_key=os.environ["DAPP_LLM_KEY"])` with model `gpt-6-luna`. With function tools, pass
  `reasoning_effort` "none" (gpt-6-luna refuses tools with reasoning in chat completions).
- `tools: [dataproduct-mcp]` (needs `llm: true`): the app may call that MCP tool at
  `$DAPP_TOOLS_URL/<tool>` with `Authorization: Bearer $DAPP_LLM_KEY` (MCP Streamable HTTP). See which tools exist
  with `dapp tool ls`.

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
Objects belong to the app (`app:<name>`); nobody else sees them unless the app shares them (`db.share(user)`).
Don't keep data in local files or SQLite on disk.

## Commands
```
dapp deploy -f dapp.yaml --dry-run     # check first
dapp deploy -f dapp.yaml               # create or update (a code change rolls the app in about a minute)
dapp --json status NAME                # wait until state is running
dapp logs NAME -n 100                  # the app's output, including [dapp-run] start-up lines
dapp access allow NAME USER...         # share a private app
dapp ls | dapp usage NAME | dapp tool ls
dapp rm NAME --yes                     # only when the user asks
```

## Finish every task like this
1. Deploy, then poll `dapp --json status NAME` until it runs (start-up takes 30-90 s).
2. Check it: `curl -s -o /dev/null -w '%{http_code}' https://$DATAOS_FQDN/NAME/` gives 302 (sign-in page) for a
   private app or 200 for a public one; read `dapp logs NAME` if it crashes and fix the cause.
3. Tell the user the URL `https://$DATAOS_FQDN/NAME/`, who can open it, and how to share it.

~/work is saved every 30 s to your immortal-obj volume `workspace` (and restored when this terminal restarts),
except caches, virtualenvs, .git, .env/keys, databases and files over 700 KB. Your Claude conversations are saved too
(`claude-sessions`); the Claude sign-in is not, so sign in again after a restart.
