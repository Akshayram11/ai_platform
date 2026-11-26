# Custom API sources with Nilus (parked)

Status: **design only, parked on 2026-11-26.** Nothing here is built or tested live. This page keeps what we learned and decided,
so the work can start from here. Today's Pipelines feature ([pipelines.md](pipelines.md)) covers sources that have a Depot (or
that we connect as one: [Sources](pipelines.md)); this page is about APIs that have neither a Depot nor a Nilus connector.

## How Nilus does custom sources (from the DataOS docs)
Source: `v2.dataos.info/references/resources/nilus/batch/custom-sources/creating-custom-source`.

- You write a Python class that subclasses `nilus.CustomSource` and implements:
  - `nilus_source(uri, table, **kwargs)`: returns one `@nilus.resource` (a generator that yields one row at a time) or a tuple of them;
  - `handles_incrementality()`: `True` only if the code keeps its own cursor in `nilus.current.resource_state()`.
- The pipeline's source address is `custom://<ClassName>?<option>=<value>`. The host is the class name (case-sensitive).
  Query parameters reach the code through `uri`; extra `source.options` keys arrive as UPPER-CASE environment variables
  (`api_key` becomes `API_KEY`); `source_table` arrives as `table`.
- Credentials: never in code or the manifest. Put them in a DataOS Secret, project them with `spec.use.projection` as env vars,
  and reference them as `{API_TOKEN}` in the address.
- Limits:
  - batch only (a schedule is fine; no CDC, no stream);
  - `sink.options.dest_table` is the destination **schema** only: each resource's name becomes a table in it;
  - runtime is Python 3.13 (Nilus 2.0.29+); `requirements.txt` beside the code is installed at start, listing only extra packages;
  - only top-level `*.py` files in `baseDir` are scanned; helpers sit beside them and are imported by module name.

### Why Nilus needs a Git repository
Every Nilus pipeline runs in its own pod, from Nilus's image. That image has the built-in connectors (PostgreSQL, Snowflake...)
but not your code. Nilus's only way to get custom code into the pod is **git-sync**: before each run it clones `spec.repo.url`
(at `baseDir`, optionally pinned with `syncFlags: ["--ref=main"]`) and imports the classes. Without `spec.repo` it fails with
`custom baseDir not set in repo`. There is no upload API, and a custom image is not part of the documented manifest.
A private repo needs a Secret with `GITSYNC_USERNAME` / `GITSYNC_PASSWORD` in `spec.repo.secretId`.

Git also gives version pinning (`--ref=v1.2.0`) and a history of every change to the connector.

## The plan we settled on: one generic connector, so users never touch Git
Most APIs are alike: REST, JSON, a token, pages. So we write **one** generic connector, `RestApiSource`, once, and keep it in a
repository. A person only fills a form; their answers become pipeline settings, not code:

1. **Where the API is**: base URL and endpoint (e.g. `https://api.example.com/v1/orders`).
2. **Auth**: none, Bearer token, API key in a header, or username/password. Credentials go into a DataOS Secret owned by the
   person (as for [Sources](pipelines.md)) and reach the pipeline through `spec.use.projection`.
3. **Where the records are** in the JSON (e.g. `data.items`). A **Try it** button fetches the first page and shows sample rows,
   and guesses this path.
4. **Pages**: none, page number, offset/limit, cursor, or a "next" link. Also guessed from the sample.
5. **Incremental** (optional): a field such as `updated_at`; the connector keeps the last value in `resource_state()`.
6. Then the usual Pipelines questions: destination (a Lakehouse schema) and schedule.

An unusual API (SOAP, odd auth, several calls joined together) gets its own connector class, written by Claude Code and added to
the same repository; that becomes a recipe in `apps/claude-code/CLAUDE.md`.

Rules for **Try it**: the dashboard calls the API itself, so it must only allow public `https` hosts (no cluster-internal names or
private IPs), otherwise it could be used to reach services inside DataOS. Credentials typed for Try it are used for that one call
and never kept.

## Where the connector code could live (decision open)

| Option | How | For | Against |
|---|---|---|---|
| **Public GitHub repo** (`Akshayram11/ai_platform`, folder `nilus-connectors/`) | `spec.repo.url: https://github.com/Akshayram11/ai_platform`, `baseDir: nilus-connectors` | Simplest; works today; no token needed because the repo is public; the connector holds no secrets | Code lives outside DataOS; changes go through GitHub |
| **Private repo** | same, plus `spec.repo.secretId` | Code not public | Every pipeline shares one GitHub token: bad for governance |
| **immortal-obj as the Git server** | Keep the connector in an immortal-obj volume object; immortal-obj serves it read-only over git's "dumb HTTP" (`info/refs`, `objects/...`) at e.g. `http://immortal-obj:8080/git/nilus-connectors.git`; `spec.repo.url` points there | Everything stays inside DataOS; people (or Claude Code) could keep their own connectors from the dashboard | Real work to build (write the repo files, serve them); **untested**: git-sync may refuse a plain `http://` in-cluster URL |
| **No Nilus: a dapp app does it** | A Python job app calls the API and writes the data itself | No Git at all | We would have to write Iceberg/Lakehouse loading, scheduling, state and retries ourselves, which Nilus gives for free |

Why the immortal-obj **Volume** can't be mounted into the Nilus pod directly:
1. Nilus loads custom code only through `spec.repo` (git-sync). `spec.use.volumes` exists, but only for scratch space
   (`DATAOS_PERSISTENT_DIR`), not for code.
2. A DataOS Volume attaches to one pod at a time; mounting immortal-obj's Volume elsewhere would take it away from immortal-obj.

Recommendation when this resumes: first a cheap test of whether Nilus's git-sync can clone from an in-cluster `http://` address
(serve a tiny repo from immortal-obj, run one `custom://` pipeline against a public no-key test API). If it works, use
immortal-obj as the Git server; if not, use the public GitHub folder.

## The repository is public: what that means
`Akshayram11/ai_platform` was found to be **public already** (2026-11-26). A full-history scan found **no real secrets**: the
`deploy/secrets/*.yaml` files hold placeholders only (`${OPENAI_API_KEY}`, `<issued by the gateway on deploy>`), and no API key,
token or private-key pattern appears in any commit. Some internal details are public, though: colleagues' DataOS user ids and
Depot/Secret names in `docs/nilus-notes.md`, a colleague's name and email in `tests/test_platform.py`, and the DataOS instance
host name in docs and config. Changing them in a new commit does not remove them from history; that would need a history rewrite
and a force-push (not done; only on an explicit decision).

## To do when this resumes
1. Decide where the connector code lives (table above); run the git-sync test if immortal-obj is the choice.
2. Live check: a `custom://` pipeline against a public no-key API reaches the internet from DataOS and lands rows in the Lakehouse.
3. Write `RestApiSource` (auth kinds, records path, pagination kinds, incremental cursor) with tests against a fake API.
4. Dashboard "Connect an API" form with Try it (public https only), then the normal pipeline questions.
5. `dapp pipeline new --from api:...` in the CLI, docs, and a CLAUDE.md recipe (including "write a connector for an unusual API").
