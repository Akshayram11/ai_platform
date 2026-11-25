# immortal-obj

Durable storage that belongs to a person or an app, our equivalent of Cloudflare Durable Objects on DataOS.
Apps lose their files on every restart; an object does not. Governed by DataOS identity: every call is made by a
known caller and recorded by the gateway.

## Model
- An **object** has one **owner** (`user:<id>` or `app:<name>`) and a name. A bare name is always your own;
  `<owner>/<name>` refers to one shared with you (for example `app:chatbot/u/alice-2bd806/chat`).
- Two kinds:
  - `db`: a SQLite file with a key-value table `_kv` (`put`, `get`, `keys`, `delete`) plus any tables you create (`sql`).
  - `volume`: a folder of files (`write_file`, `read_file`, `ls`, `remove`); created by the first write.
- One writer at a time per object (a lock per file), like a Durable Object.
- **Sharing:** the owner shares an object with a person or `app:<name>`, read or write (`share`, `unshare`). Shares are
  kept in immortal-obj's own table (not in Heimdall: see "Not yet").
- Storage: one SQLite file per object plus `meta.sqlite` (objects and shares) on the DataOS Volume `immortal-obj`
  (`/data`, 5 Gi). Names are lower-case, with `/` for up to four levels; paths are relative with no `..`.

## Limits and guards
| What | Limit |
|---|---|
| Object size | 200 MiB |
| One file in a volume object | 700,000 bytes |
| Query time | 5 s |
| Rows returned by `sql` | 500 (flagged `truncated`) |
| Statement | one per call; `ATTACH`, `DETACH`, `PRAGMA`, `load_extension`, `readfile`, `writefile` are denied, `VACUUM` is rejected |

## Who can call
- People and Claude Code: the MCP tool `immortal-obj` through the gateway (`dapp tool connect immortal-obj`); the dashboard
  **Storage** page does the same as you.
- Apps: `objects: true` in `dapp.yaml` (implies `llm: true`; sets `DAPP_OBJECTS_URL`), then:

```python
from dapp_objects import objects            # already in the runtime image
db = objects.open("history")                # this app's own object, created on first write
db.put("settings", {"theme": "dark"}); db.get("settings")
db.sql("create table if not exists msgs (who text, text text, at text default current_timestamp)")
db.sql("insert into msgs (who, text) values (?, ?)", [user, text])
rows = db.sql("select * from msgs where who = ? order by at desc limit 20", [user])    # list of dicts
mine = objects.open("chat", for_user=user)  # one object per signed-in person (named u/<slug>-<hash>/chat)
files = objects.volume("workspace")         # files: write, read, ls, remove, push(folder), pull(folder)
db.share(user)                              # let a person read it
```
Apps that are not built from the runtime image copy `src/dapp_objects.py` into their image (the chatbot Dockerfile does).
The helper sends a `User-Agent` because Cloudflare blocks the default Python one.

## Where we use it
| Use | Object | Notes |
|---|---|---|
| Chatbot history | `app:chatbot` → `u/<person>/chat` (tables `chats`, `msgs`) | Shared back with the person read-only, so they see it on the dashboard Storage page |
| Claude Code workspace | `user:<you>/workspace` (`~/work`) and `claude-sessions` (`~/.claude/projects`) | Saved every 30 s and once more at shutdown; restored at start |
| Access requests | `app:dapp` → `access-requests` | Only the dashboard's routes read or write it |

## Setup
Volume and app are in `apps/immortal-obj/` (see its `dapp.yaml` comments). The tool token `TOOL_TOKEN` is in
`apps/immortal-obj/.env` (gitignored) and the secret `dapp-immortal-obj-token`; register the tool with
`(set -a; . apps/immortal-obj/.env; set +a; dapp tool add immortal-obj --url http://immortal-obj:8080/mcp --auth-env TOOL_TOKEN …)`.
The gateway must list `immortal-obj` in `DAPP_TOOL_INTERNAL_HOSTS`, and the tool is allowed for `*` and `app:*`.

## Not yet
- Sharing is checked by immortal-obj, not by DataOS/Heimdall policies. Heimdall's authorize endpoint did not answer
  "may X use resource Y" for us; ask the platform team for a permission API before relying on it for strong governance.
- No alarms or scheduling (Durable Object alarms), no cross-object transactions.
