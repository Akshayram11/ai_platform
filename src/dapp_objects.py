"""Durable storage for dapp apps: objects in immortal-obj, through the dapp gateway.

In dapp.yaml:  objects: true          (the app gets DAPP_OBJECTS_URL and its gateway key DAPP_LLM_KEY)

    from dapp_objects import objects

    db = objects.open("settings")                   # the app's own object (created on first write)
    db.put("theme", {"dark": True})
    db.get("theme")                                 # {'dark': True}
    db.sql("create table if not exists msgs (who text, text text, at text default current_timestamp)")
    db.sql("insert into msgs (who, text) values (?, ?)", [user, text])
    db.sql("select * from msgs where who = ? order by at desc limit 20", [user])   # list of dicts

    mine = objects.open("chat", for_user=user)      # one object per signed-in person (x-dapp-user in a private app)

    vol = objects.volume("reports")                 # a volume object: a folder of files
    vol.write("2026/q3.md", "# Q3")
    vol.read_text("2026/q3.md"); vol.ls("2026/")
    vol.push("/tmp/out"); vol.pull("/tmp/in")       # sync a local folder up / down (only changed files move)

Objects belong to the app (app:<name>); other apps and people see them only if the app shares them. Standard
library only, so it also works outside the runtime image.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL = "2025-06-18"
MAX_FILE = 700_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".npm", ".local"}
SKIP_FILES = re.compile(r"(^\.env(\..*)?$|\.pyc$|\.pem$|\.key$|^id_[a-z0-9]+(\.pub)?$|\.sqlite(-wal|-shm)?$|\.db$)")


class ObjectsError(RuntimeError):
    pass


class Client:
    def __init__(self, url: str | None = None, key: str | None = None, *, timeout: float = 30, opener=None):
        self.url = url or os.environ.get("DAPP_OBJECTS_URL", "")
        self.key = key or os.environ.get("DAPP_LLM_KEY", "")
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._id = 0

    def call(self, tool: str, args: dict, *, end_user: str | None = None) -> Any:
        if not self.url or not self.key:
            raise ObjectsError("no DAPP_OBJECTS_URL / DAPP_LLM_KEY: add `objects: true` to dapp.yaml and redeploy")
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.key}", "MCP-Protocol-Version": PROTOCOL,
                   "User-Agent": "dapp-objects/0.1"}   # the edge refuses urllib's default agent
        if end_user:
            headers["x-dapp-user"] = end_user
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise ObjectsError(f"immortal-obj: HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        msg = json.loads(_first_event(raw))
        if "error" in msg:
            raise ObjectsError(f"immortal-obj: {msg['error'].get('message')}")
        res = msg.get("result") or {}
        if res.get("isError"):
            text = " ".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
            raise ObjectsError(text or "immortal-obj refused the call")
        out = res.get("structuredContent")
        if isinstance(out, dict) and set(out) == {"result"}:
            return out["result"]
        if out is not None:
            return out
        texts = [c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text"]
        if len(texts) != 1:
            return texts
        try:
            return json.loads(texts[0])     # tools returning a plain dict send it as JSON text
        except ValueError:
            return texts[0]

    def open(self, name: str, *, for_user: str | None = None) -> Object:
        """An object by name. for_user: keep one object per end user of the app, e.g. their chat history."""
        return Object(self, _per_user(name, for_user), for_user)

    def volume(self, name: str, *, for_user: str | None = None) -> Volume:
        """A volume object (a folder of files) by name; for_user as in open()."""
        return Volume(self, _per_user(name, for_user), for_user)

    def list(self) -> dict:
        return self.call("objects", {})


class Object:
    def __init__(self, client: Client, name: str, end_user: str | None = None):
        self.client, self.name, self.end_user = client, name, end_user

    def _call(self, tool: str, **args) -> Any:
        return self.client.call(tool, {"object": self.name, **args}, end_user=self.end_user)

    def put(self, key: str, value: Any) -> None:
        self._call("put", key=key, value=json.dumps(value))

    def get(self, key: str, default: Any = None) -> Any:
        value = json.loads(self._call("get", key=key))
        return default if value is None else value

    def keys(self, prefix: str = "", limit: int = 100) -> list[str]:
        return self._call("keys", prefix=prefix, limit=limit)

    def delete(self, key: str) -> None:
        self._call("delete", key=key)

    def sql(self, query: str, params: list | None = None) -> list[dict]:
        """Rows as dicts for a select; [] for statements that return nothing."""
        r = self._call("sql", query=query, params=list(params or []))
        return [dict(zip(r["columns"], row, strict=True)) for row in r["rows"]]

    def share(self, who: str, mode: str = "read") -> dict:
        return self._call("share", who=who, mode=mode)

    def drop(self) -> None:
        self._call("drop")


class Volume:
    """A volume object: files by path. Created by the first write."""

    def __init__(self, client: Client, name: str, end_user: str | None = None):
        self.client, self.name, self.end_user = client, name, end_user

    def _call(self, tool: str, **args) -> Any:
        return self.client.call(tool, {"object": self.name, **args}, end_user=self.end_user)

    def write(self, path: str, data: bytes | str) -> dict:
        raw = data.encode() if isinstance(data, str) else bytes(data)
        return self._call("write_file", path=path, content=base64.b64encode(raw).decode(), encoding="base64")

    def read(self, path: str) -> bytes:
        r = self._call("read_file", path=path, encoding="base64")
        return base64.b64decode(r["content"])

    def read_text(self, path: str) -> str:
        return self.read(path).decode()

    def ls(self, prefix: str = "") -> list[dict]:
        return self._call("ls", prefix=prefix)

    def remove(self, path: str, recursive: bool = False) -> None:
        self._call("remove", path=path, recursive=recursive)

    def push(self, folder: str | os.PathLike, *, delete: bool = True) -> dict:
        """Make the volume match a local folder: upload new/changed files, and (delete=True) remove files that are
        gone locally. Skips caches, virtualenvs, .git, secrets (.env, keys) and files over 700 KB."""
        local, skipped = _scan(Path(folder))
        remote = {f["path"]: f["sha256"] for f in self.ls()}
        sent = [p for p, (sha, _) in local.items() if remote.get(p) != sha]
        for p in sent:
            self.write(p, local[p][1].read_bytes())
        gone = [p for p in remote if p not in local] if delete else []
        for p in gone:
            self.remove(p)
        return {"uploaded": len(sent), "removed": len(gone), "unchanged": len(local) - len(sent), "skipped": skipped}

    def pull(self, folder: str | os.PathLike) -> dict:
        """Bring the volume's files into a local folder (only those that differ). Local extras are left alone."""
        root = Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        got = 0
        files = self.ls()
        for f in files:
            dest = root / f["path"]
            if root.resolve() not in dest.resolve().parents:
                continue                                 # never write outside the folder
            if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == f["sha256"]:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.read(f["path"]))
            got += 1
        return {"downloaded": got, "unchanged": len(files) - got}


def _scan(root: Path) -> tuple[dict[str, tuple[str, Path]], list[str]]:
    files, skipped = {}, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not Path(dirpath, d).is_symlink())
        for fn in sorted(filenames):
            p = Path(dirpath, fn)
            rel = p.relative_to(root).as_posix()
            if p.is_symlink() or not p.is_file() or SKIP_FILES.search(fn):
                continue
            if p.stat().st_size > MAX_FILE:
                skipped.append(rel)
                continue
            files[rel] = (hashlib.sha256(p.read_bytes()).hexdigest(), p)
    return files, skipped


def _per_user(name: str, for_user: str | None) -> str:
    if not for_user:
        return name
    slug = re.sub(r"[^a-z0-9]+", "-", for_user.lower()).strip("-")[:40] or "user"
    return f"u/{slug}-{hashlib.sha256(for_user.encode()).hexdigest()[:6]}/{name}"


def _first_event(raw: str) -> str:
    """The JSON-RPC message, whether the server answered plain JSON or one server-sent event."""
    raw = raw.strip()
    if raw.startswith("{"):
        return raw
    data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    if not data:
        raise ObjectsError(f"immortal-obj: unexpected reply {raw[:200]!r}")
    return data[0]


objects = Client()
