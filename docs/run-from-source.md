# Run an app from source: no Docker image

For Python apps. You write a folder and a `dapp.yaml`; there is no Dockerfile, no registry, no build.

```yaml
name: sales-dash                 # lower-case, digits, '-'; the app is served at /sales-dash/
description: Sales dashboard
runtime: python-3.12             # pre-built image docker.io/tmdcio/dapp-runtime:python-3.12-rN
code: ./app                      # packed and shipped by dapp deploy
run: streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true --server.baseUrlPath sales-dash
compute: engineering-compute
resources: {cpu: 100m, memory: 256Mi, cpuLimit: "1", memoryLimit: 768Mi}
# access: {users: [alice, bob@corp.io]}     # private by default; owners always in
# public: true                              # only if anyone with the URL may open it
```

## What happens on `dapp deploy`
1. **Pack.** `app/` becomes a deterministic tar.gz (sorted names, no timestamps) up to **~700 KB**. Skipped:
   `.git`, `.venv`, `__pycache__`, `node_modules`, caches, `.env*` (except `.env.example`), `*.pem`, `*.key`, `id_rsa*`,
   `*.db`, `*.sqlite`.
2. **Ship.** The archive is base64'd into pieces of 100,000 characters and stored in the DataOS secret
   `dapp-<app>-code` (`DAPP_CODE_0…`, `DAPP_CODE_SHA256`), because a Kubernetes secret is capped at 1 MiB and an env var at
   128 KiB. `run:` travels as `DAPP_RUN`.
3. **Start.** The container runs `python /opt/dapp/dapp_run.py`: it rejoins the pieces, checks the sha256, unpacks into
   `/home/app/code` (or another writable folder), installs `requirements.txt` on top of the libraries already in the image
   (with `uv`), starts the CPU/memory endpoint, then runs `DAPP_RUN` and stays its parent so signals and exit codes pass through.
4. **Same code, same hash.** Redeploying unchanged code does not restart the app; a change rolls in about a minute.

Private apps (the default): the app listens on 8081 and `dapp_proxy` serves 8080 with DataOS sign-in in front (pages and
websockets), so any web server gets login without code changes. Public apps listen on 8080 directly.
The signed-in person's id arrives in the `x-dapp-user` request header.

## What the image has
Python 3.12, uv, fastapi, uvicorn, streamlit, pandas, numpy, plotly, httpx, requests, openai, anthropic, pydantic, jinja2 (list in
`runtimes/python-base.txt`), plus the dapp helpers in `/opt/dapp`: `dapp_run`, `dapp_login`, `dapp_proxy`, `dapp_metrics`,
`dapp_objects`. Anything else goes in `app/requirements.txt` (installed at start, so the first start is 30-90 s).

## Rules of thumb
- Listen on `$PORT` on `0.0.0.0`; the app lives under `/<name>/`: Streamlit `--server.baseUrlPath <name>`; FastAPI
  `FastAPI(root_path=os.environ.get("ROOT_PATH", ""))`, `env: {ROOT_PATH: /<name>}`, `uvicorn … --proxy-headers`.
- Python only, no system packages, no data files in `code:` (keep data in [immortal-obj](immortal-obj.md)).
- Never put secrets in code or `dapp.yaml`.
- `llm: true`, `tools: [..]`, `objects: true`, `metrics: true` work the same as for image apps.

## Runtime images
Pinned by `RUNTIME_REVISION` in `src/dapp/spec.py` (now `r8`). Rebuild:
```bash
docker buildx build --platform linux/amd64 -f runtimes/python.Dockerfile --build-arg PY=3.12 \
  -t docker.io/tmdcio/dapp-runtime:python-3.12-r8 -t docker.io/tmdcio/dapp-runtime:python-3.12 --push .
```
then bump `RUNTIME_REVISION`. Apps pick up a new revision on their next `dapp deploy`.

## Why it exists
The Claude Code pod has no Docker. With this, an agent only writes files and runs `dapp deploy`. See [claude-code.md](claude-code.md).
