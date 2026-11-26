"""immortal-obj MCP server (Streamable HTTP), reached only through the dapp gateway.

The gateway checks who is calling (DataOS sign-in for people, gateway keys for apps), records every call, and
forwards it here with TOOL_TOKEN plus x-dapp-caller. Without TOOL_TOKEN we answer nothing, so nobody can claim
to be someone else by calling us directly.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import hmac
import json
import os

import uvicorn
from immortal_obj import Caller, ObjError, Store
from landing import PAGE
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

TOKEN = os.environ.get("TOOL_TOKEN", "")
store = Store(os.environ.get("IMMORTAL_DATA", "/data/immortal-obj"))
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="")

mcp = MCPServer("immortal-obj", instructions=(
    "Durable storage that is yours: objects (small databases) that survive restarts and redeploys. "
    "Use a bare name ('notes', 'chat/2026-11') for your own object; 'user:<id>/<name>' or 'app:<name>/<name>' "
    "for one someone shared with you. put/get/keys/delete keep JSON values by key; sql runs SQLite on the object "
    "(create your own tables). A volume object is a folder of files instead: write_file/read_file/ls/remove "
    "(created by the first write_file). Call objects to see what you have."))


def me() -> Caller:
    try:
        return Caller.from_gateway(_caller.get())
    except ObjError as e:
        raise ToolError(str(e)) from e


def run(fn, *args):
    try:
        return fn(me(), *args)
    except ObjError as e:
        raise ToolError(str(e)) from e


@mcp.tool()
def put(object: str, key: str, value: str) -> dict:
    """Save a value under a key in an object (created on first put). value is JSON text, e.g. '"hi"' or
    '{"a": 1}'; plain text that isn't JSON is stored as a string."""
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    return run(store.put, object, key, parsed)


@mcp.tool()
def get(object: str, key: str) -> str:
    """Read the value under a key (JSON text; null when missing)."""
    return json.dumps(run(store.get, object, key))


@mcp.tool()
def keys(object: str, prefix: str = "", limit: int = 100) -> list[str]:
    """Keys in an object, optionally only those starting with prefix."""
    return run(store.keys, object, prefix, limit)


@mcp.tool()
def delete(object: str, key: str) -> dict:
    """Remove one key from an object."""
    return run(store.delete, object, key)


@mcp.tool()
def sql(object: str, query: str, params: list | None = None) -> dict:
    """Run one SQLite statement on an object, e.g. create table / insert / select. Use ? placeholders with params.
    Returns columns and up to 500 rows. The key-value pairs live in table _kv (k, v, updated)."""
    return run(store.sql, object, query, params or [])


@mcp.tool()
def write_file(object: str, path: str, content: str, encoding: str = "text") -> dict:
    """Write a file into a volume object (created on first write), e.g. path "src/app.py". encoding "text" (UTF-8)
    or "base64" for binary. Up to 700 KB per file."""
    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ToolError(f"content is not valid base64: {e}") from e
    elif encoding == "text":
        data = content.encode()
    else:
        raise ToolError('encoding is "text" or "base64"')
    return run(store.write_file, object, path, data)


@mcp.tool()
def read_file(object: str, path: str, encoding: str = "text") -> dict:
    """Read a file from a volume object. Text comes back as is; a file that isn't UTF-8 (or encoding "base64")
    comes back base64."""
    data = run(store.read_file, object, path)
    if encoding == "text":
        try:
            return {"path": path, "encoding": "text", "content": data.decode()}
        except UnicodeDecodeError:
            pass
    return {"path": path, "encoding": "base64", "content": base64.b64encode(data).decode()}


@mcp.tool()
def ls(object: str, prefix: str = "") -> list[dict]:
    """Files in a volume object (all, or under a folder like "src/"), with size and sha256."""
    return run(store.ls, object, prefix)


@mcp.tool()
def remove(object: str, path: str, recursive: bool = False) -> dict:
    """Delete a file from a volume object; recursive=true also deletes a folder's contents."""
    return run(store.remove, object, path, recursive)


@mcp.tool()
def objects() -> dict:
    """Your objects (and who you shared them with) and the objects others shared with you."""
    return run(store.objects)


@mcp.tool()
def drop(object: str) -> dict:
    """Delete one of your objects with everything in it. Cannot be undone."""
    return run(store.drop, object)


@mcp.tool()
def share(object: str, who: str, mode: str = "read") -> dict:
    """Let someone use one of your objects: who is a DataOS user id or app:<name>; mode read or write."""
    return run(store.share, object, who, mode)


@mcp.tool()
def unshare(object: str, who: str) -> dict:
    """Stop sharing one of your objects with someone."""
    return run(store.unshare, object, who)


def build():
    hosts = [h.strip() for h in os.environ.get("TOOL_ALLOWED_HOSTS", "immortal-obj:8080,immortal-obj,localhost:8080")
             .split(",") if h.strip()]
    inner = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0",
                                    transport_security=TransportSecuritySettings(allowed_hosts=hosts))

    async def app(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"].endswith("/healthz"):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": b'{"ok": true}'})
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            if (scope["method"] == "GET" and scope["path"].rstrip("/").rsplit("/", 1)[-1] in ("", "immortal-obj")
                    and "text/html" in headers.get("accept", "")):
                body = PAGE.encode()          # the guide page: static text, no data
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                return await send({"type": "http.response.body", "body": body})
            given = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not TOKEN or not hmac.compare_digest(given.encode(), TOKEN.encode()):
                body = b'{"error": "immortal-obj is reached through the dapp gateway"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                return await send({"type": "http.response.body", "body": body})
            token = _caller.set(headers.get("x-dapp-caller", ""))
            try:
                return await inner(scope, receive, send)
            finally:
                _caller.reset(token)
        return await inner(scope, receive, send)

    return app


app = build()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
