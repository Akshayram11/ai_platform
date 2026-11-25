"""immortal-obj: durable objects for agents and apps, kept on a DataOS Volume.

An object is a small SQLite database with one owner, of one of two kinds:

    db       a key-value table plus the owner's own SQL tables (put/get/keys/delete/sql)
    volume   a folder of files (write_file/read_file/ls/remove), e.g. an agent's workspace

 Owners are DataOS identities as the dapp gateway proved them:
a person ("user:<id>") or an app ("app:<name>"). Every object has a key-value table and room for the owner's own
SQL tables. Nothing here decides who a caller is: the gateway does (Heimdall for people, gateway keys for apps) and
tells us in headers only it can set. Here we only decide what that caller may do:

    own objects        everything (put/get/sql/drop/share)
    shared with them   read  -> get, keys, read-only SQL
                       write -> also put, delete, writing SQL

Object references: a bare name ("notes", "chats/2026") is the caller's own object; "<owner>/<name>"
("user:alice/notes", "app:chatbot/history") names someone else's, which works only if they shared it.

Layout on the volume (IMMORTAL_DATA, default /data/immortal-obj):
    meta.sqlite                    objects and shares
    objects/<hash>.sqlite          one file per object (the hash of owner + name, so names never become paths)

One object = one lock = one writer at a time, like a Durable Object: requests to the same object run one after
another, different objects run side by side.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63}){0,3}$")
OWNER_RE = re.compile(r"^(user:[A-Za-z0-9_.@-]{1,128}|app:[a-z0-9][a-z0-9-]{0,47})$")
MAX_KEY = 512
MAX_VALUE = 1_000_000          # one JSON value
MAX_ROWS = 500                 # rows a sql call returns
MAX_OBJECT_PAGES = 51_200      # x 4 KiB pages = 200 MiB per object
SQL_SECONDS = 5.0
KV_TABLE = "_kv"
FILES_TABLE = "_files"
KINDS = ("db", "volume")
MAX_FILE = 700_000             # bytes per file: it travels base64 in one JSON-RPC call (the gateway takes 1 MB)
MAX_PATH = 512


class ObjError(Exception):
    """A refusal or bad request, shown to the caller as is."""


@dataclass(frozen=True)
class Caller:
    owner: str                  # "user:<id>" or "app:<name>"

    @classmethod
    def from_gateway(cls, caller: str) -> Caller:
        caller = (caller or "").strip()
        owner = caller if caller.startswith("app:") else f"user:{caller}"
        if not caller or not OWNER_RE.match(owner):
            raise ObjError("no caller: immortal-obj is reached through the dapp gateway")
        return cls(owner)


def _clean_name(name: str) -> str:
    name = (name or "").strip().strip("/")
    if not NAME_RE.match(name) or ".." in name:
        raise ObjError(f"bad object name {name!r}: lower-case letters, digits, . _ - and up to 3 '/'")
    return name


def _clean_path(path: str) -> str:
    """A file path inside a volume object: relative, '/'-separated, no '..', no empty or control-char parts."""
    p = (path or "").strip()
    parts = p.split("/")
    if (not p or len(p) > MAX_PATH or p.startswith("/") or "\\" in p
            or any(part in ("", ".", "..") for part in parts) or any(ord(ch) < 32 for ch in p)):
        raise ObjError(f"bad file path {path!r}: relative, '/'-separated, no '..'")
    return p


def _deny_dangerous(action, arg1, arg2, dbname, source):  # sqlite3 authorizer for caller SQL
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA):
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and arg2 in ("load_extension", "readfile", "writefile"):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        self._meta_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        with self._meta() as db:
            db.executescript("""
                create table if not exists objects (
                    owner text not null, name text not null, file text not null,
                    created real not null, updated real not null, kind text not null default 'db',
                    primary key (owner, name));
                create table if not exists shares (
                    owner text not null, name text not null, who text not null,
                    mode text not null check (mode in ('read', 'write')), granted real not null,
                    primary key (owner, name, who));
            """)
            if "kind" not in [r[1] for r in db.execute("pragma table_info(objects)")]:   # before volumes existed
                db.execute("alter table objects add column kind text not null default 'db'")

    # ------------------------------------------------------------ plumbing
    @contextmanager
    def _meta(self):
        with self._meta_lock, closing(sqlite3.connect(self.root / "meta.sqlite")) as db:
            db.execute("pragma journal_mode=wal")
            with db:
                yield db

    def _lock(self, file: str) -> threading.Lock:
        with self._meta_lock:
            return self._locks.setdefault(file, threading.Lock())

    @staticmethod
    def _file(owner: str, name: str) -> str:
        return hashlib.sha256(f"{owner}\0{name}".encode()).hexdigest()[:40] + ".sqlite"

    def _resolve(self, caller: Caller, ref: str, need: str) -> tuple[str, str, str]:
        """(owner, name, mode) for a reference, or refuse. need: read | write | own."""
        ref = (ref or "").strip()
        owner, name = caller.owner, ref
        if "/" in ref and ref.split("/", 1)[0].startswith(("user:", "app:")):
            owner, name = ref.split("/", 1)
            if not OWNER_RE.match(owner):
                raise ObjError(f"bad owner in {ref!r}")
        name = _clean_name(name)
        if owner == caller.owner:
            return owner, name, "own"
        with self._meta() as db:
            row = db.execute("select mode from shares where owner=? and name=? and who=?",
                             (owner, name, caller.owner)).fetchone()
        mode = row[0] if row else None
        if need == "own" or mode is None or (need == "write" and mode != "write"):
            # same answer whether or not the object exists: don't reveal other people's object names
            raise ObjError(f"{ref!r} is not yours and not shared with you"
                           + (" for writing" if mode == "read" and need == "write" else ""))
        return owner, name, mode

    @contextmanager
    def _open(self, owner: str, name: str, *, create: bool, read_only: bool = False, kind: str = "db"):
        with self._meta() as db:
            row = db.execute("select file, kind from objects where owner=? and name=?", (owner, name)).fetchone()
            if row is None:
                if not create:
                    raise ObjError(f"no object {name!r}" + ("" if owner.startswith("user:") else f" for {owner}"))
                now = time.time()
                file = self._file(owner, name)
                db.execute("insert into objects (owner, name, file, created, updated, kind) values (?, ?, ?, ?, ?, ?)",
                           (owner, name, file, now, now, kind))
            else:
                file, have = row
                if have != kind:
                    use = "write_file/read_file/ls/remove" if have == "volume" else "put/get/keys/delete/sql"
                    raise ObjError(f"{name!r} is a {have} object: use {use}")
        path = self.root / "objects" / file
        with self._lock(file):
            db = sqlite3.connect(path, isolation_level=None)
            try:
                db.execute("pragma journal_mode=wal")
                db.execute(f"pragma max_page_count={MAX_OBJECT_PAGES}")
                if kind == "volume":
                    db.execute(f"create table if not exists {FILES_TABLE} (path text primary key, data blob not null, "
                               "size integer not null, sha256 text not null, updated real not null)")
                else:
                    db.execute(f"create table if not exists {KV_TABLE} (k text primary key, v text not null, "
                               "updated real not null)")
                if read_only:
                    db.execute("pragma query_only=1")
                yield db
            finally:
                db.close()
            if not read_only:
                with self._meta() as m:
                    m.execute("update objects set updated=? where owner=? and name=?", (time.time(), owner, name))

    # ------------------------------------------------------------ key-value
    def put(self, caller: Caller, obj: str, key: str, value) -> dict:
        if not key or len(key) > MAX_KEY:
            raise ObjError(f"key must be 1-{MAX_KEY} characters")
        text = json.dumps(value)
        if len(text) > MAX_VALUE:
            raise ObjError(f"value over {MAX_VALUE} bytes as JSON")
        owner, name, _ = self._resolve(caller, obj, "write")
        with self._open(owner, name, create=owner == caller.owner) as db:
            db.execute(f"insert into {KV_TABLE} values (?, ?, ?) on conflict(k) do update set v=excluded.v, "
                       "updated=excluded.updated", (key, text, time.time()))
        return {"object": f"{owner}/{name}", "key": key, "saved": True}

    def get(self, caller: Caller, obj: str, key: str, default=None):
        owner, name, _ = self._resolve(caller, obj, "read")
        try:
            with self._open(owner, name, create=False, read_only=True) as db:
                row = db.execute(f"select v from {KV_TABLE} where k=?", (key,)).fetchone()
        except ObjError as e:
            if "no object" in str(e):
                return default
            raise
        return json.loads(row[0]) if row else default

    def delete(self, caller: Caller, obj: str, key: str) -> dict:
        owner, name, _ = self._resolve(caller, obj, "write")
        with self._open(owner, name, create=False) as db:
            n = db.execute(f"delete from {KV_TABLE} where k=?", (key,)).rowcount
        return {"deleted": n}

    def keys(self, caller: Caller, obj: str, prefix: str = "", limit: int = 100) -> list[str]:
        owner, name, _ = self._resolve(caller, obj, "read")
        limit = max(1, min(int(limit), 1000))
        like = _like_prefix(prefix)
        try:
            with self._open(owner, name, create=False, read_only=True) as db:
                return [r[0] for r in db.execute(f"select k from {KV_TABLE} where k like ? escape '\\' order by k "
                                                 "limit ?", (like, limit))]
        except ObjError as e:
            if "no object" in str(e):
                return []
            raise

    # ------------------------------------------------------------ SQL
    def sql(self, caller: Caller, obj: str, query: str, params: list | None = None) -> dict:
        if re.search(r"\bvacuum\b", query, re.IGNORECASE):   # VACUUM INTO '<path>' would write a file anywhere on the volume
            raise ObjError("VACUUM is not allowed")
        owner, name, mode = self._resolve(caller, obj, "read")
        read_only = mode == "read"
        with self._open(owner, name, create=owner == caller.owner, read_only=read_only) as db:
            deadline = time.monotonic() + SQL_SECONDS
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            db.set_authorizer(_deny_dangerous)
            try:
                cur = db.execute(query, list(params or []))
                rows = cur.fetchmany(MAX_ROWS + 1) if cur.description else []
            except sqlite3.Error as e:
                msg = str(e)
                if "interrupted" in msg:
                    msg = f"query ran over {SQL_SECONDS:.0f}s"
                elif "readonly" in msg or "query_only" in msg:
                    msg = "this object is shared with you read-only"
                raise ObjError(f"sql error: {msg}") from e
            finally:
                db.set_authorizer(None)
                db.set_progress_handler(None, 0)
        cols = [d[0] for d in cur.description] if cur.description else []
        return {"columns": cols, "rows": [list(r) for r in rows[:MAX_ROWS]], "truncated": len(rows) > MAX_ROWS,
                "changes": cur.rowcount if not cols else 0}

    # ------------------------------------------------------------ volume objects: files
    def write_file(self, caller: Caller, obj: str, path: str, data: bytes) -> dict:
        path = _clean_path(path)
        if len(data) > MAX_FILE:
            raise ObjError(f"{path}: {len(data)} bytes, over the {MAX_FILE} byte limit for one file")
        owner, name, _ = self._resolve(caller, obj, "write")
        sha = hashlib.sha256(data).hexdigest()
        with self._open(owner, name, create=owner == caller.owner, kind="volume") as db:
            parts = path.split("/")
            parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
            clash = db.execute(f"select path from {FILES_TABLE} where path like ? escape '\\' or path in "
                               f"({','.join('?' * len(parents)) or 'null'}) limit 1",
                               (_like_prefix(path + "/"), *parents)).fetchone()
            if clash:
                raise ObjError(f"{path}: {clash[0]} is in the way (a file and a folder can't share a name)")
            try:
                db.execute(f"insert into {FILES_TABLE} values (?, ?, ?, ?, ?) on conflict(path) do update set "
                           "data=excluded.data, size=excluded.size, sha256=excluded.sha256, updated=excluded.updated",
                           (path, data, len(data), sha, time.time()))
            except sqlite3.OperationalError as e:
                raise ObjError(f"{name!r} is full ({e})") from e
        return {"object": f"{owner}/{name}", "path": path, "size": len(data), "sha256": sha}

    def read_file(self, caller: Caller, obj: str, path: str) -> bytes:
        path = _clean_path(path)
        owner, name, _ = self._resolve(caller, obj, "read")
        with self._open(owner, name, create=False, read_only=True, kind="volume") as db:
            row = db.execute(f"select data from {FILES_TABLE} where path=?", (path,)).fetchone()
        if row is None:
            raise ObjError(f"no file {path!r} in {name!r}")
        return bytes(row[0])

    def ls(self, caller: Caller, obj: str, prefix: str = "") -> list[dict]:
        """Every file under prefix (a folder like 'src/' or a start of a path), with size and sha256."""
        owner, name, _ = self._resolve(caller, obj, "read")
        try:
            with self._open(owner, name, create=False, read_only=True, kind="volume") as db:
                rows = db.execute(f"select path, size, sha256, updated from {FILES_TABLE} where path like ? "
                                  "escape '\\' order by path", (_like_prefix(prefix),)).fetchall()
        except ObjError as e:
            if "no object" in str(e):
                return []
            raise
        return [{"path": p, "size": z, "sha256": h, "updated": u} for p, z, h, u in rows]

    def remove(self, caller: Caller, obj: str, path: str, recursive: bool = False) -> dict:
        path = _clean_path(path)
        owner, name, _ = self._resolve(caller, obj, "write")
        with self._open(owner, name, create=False, kind="volume") as db:
            n = db.execute(f"delete from {FILES_TABLE} where path=?", (path,)).rowcount
            if recursive:
                n += db.execute(f"delete from {FILES_TABLE} where path like ? escape '\\'",
                                (_like_prefix(path + "/"),)).rowcount
        return {"removed": n}

    # ------------------------------------------------------------ objects and sharing
    def objects(self, caller: Caller) -> dict:
        with self._meta() as db:
            own = [{"name": n, "kind": k, "created": c, "updated": u} for n, k, c, u in
                   db.execute("select name, kind, created, updated from objects where owner=? order by name",
                              (caller.owner,))]
            for o in own:
                o["shared_with"] = [{"who": w, "mode": m} for w, m in db.execute(
                    "select who, mode from shares where owner=? and name=? order by who", (caller.owner, o["name"]))]
            shared = [{"object": f"{o}/{n}", "mode": m} for o, n, m in db.execute(
                "select owner, name, mode from shares where who=? order by owner, name", (caller.owner,))]
        return {"you": caller.owner, "own": own, "shared_with_you": shared}

    def drop(self, caller: Caller, obj: str) -> dict:
        owner, name, _ = self._resolve(caller, obj, "own")
        with self._meta() as db:
            row = db.execute("select file from objects where owner=? and name=?", (owner, name)).fetchone()
            if row is None:
                raise ObjError(f"no object {name!r}")
            db.execute("delete from objects where owner=? and name=?", (owner, name))
            db.execute("delete from shares where owner=? and name=?", (owner, name))
        with self._lock(row[0]):
            for suffix in ("", "-wal", "-shm"):
                (self.root / "objects" / (row[0] + suffix)).unlink(missing_ok=True)
        return {"dropped": f"{owner}/{name}"}

    def share(self, caller: Caller, obj: str, who: str, mode: str = "read") -> dict:
        owner, name, _ = self._resolve(caller, obj, "own")
        if mode not in ("read", "write"):
            raise ObjError("mode is read or write")
        grantee = Caller.from_gateway(who).owner
        if grantee == owner:
            raise ObjError("you already own it")
        with self._meta() as db:
            if not db.execute("select 1 from objects where owner=? and name=?", (owner, name)).fetchone():
                raise ObjError(f"no object {name!r}")
            db.execute("insert into shares values (?, ?, ?, ?, ?) on conflict(owner, name, who) do update set "
                       "mode=excluded.mode, granted=excluded.granted", (owner, name, grantee, mode, time.time()))
        return {"object": f"{owner}/{name}", "shared_with": grantee, "mode": mode}

    def unshare(self, caller: Caller, obj: str, who: str) -> dict:
        owner, name, _ = self._resolve(caller, obj, "own")
        grantee = Caller.from_gateway(who).owner
        with self._meta() as db:
            n = db.execute("delete from shares where owner=? and name=? and who=?", (owner, name, grantee)).rowcount
        return {"object": f"{owner}/{name}", "removed": n}
