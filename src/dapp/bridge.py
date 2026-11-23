"""stdio <-> Streamable HTTP bridge for gateway tools.

Claude Code (or any MCP client) starts `dapp tool bridge NAME` as a local stdio server. Each JSON-RPC message it
writes is POSTed to the gateway's /mcp/<tenant>/<tool> with the caller's *current* DataOS credentials (re-read
for every message, so a `dataos-ctl login` renewal is picked up without restarting Claude Code); replies, JSON
or SSE, are written back as one JSON message per line.

Kept deliberately small: messages are handled one at a time, and server-initiated GET streams are not opened
(tools that only answer requests, the common case, work fully).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from typing import IO, Any

import httpx

ACCEPT = "application/json, text/event-stream"


def _error(rid: Any, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": message}}


def _ids(line: str) -> list[Any]:
    try:
        msg = json.loads(line)
    except ValueError:
        return []
    items = msg if isinstance(msg, list) else [msg]
    return [m["id"] for m in items if isinstance(m, dict) and "id" in m and "method" in m]


def _sse_messages(lines: Iterable[str]) -> Iterable[str]:
    data: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            yield "\n".join(data)
            data = []
    if data:
        yield "\n".join(data)


def run(url: str, headers: Callable[[], dict[str, str]], *, stdin: IO[str] | None = None,
        stdout: IO[str] | None = None, transport: httpx.BaseTransport | None = None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    session: str | None = None
    protocol: str | None = None

    def emit(obj: Any) -> None:
        if isinstance(obj, str):  # stdio framing is one message per line: re-encode pretty-printed JSON
            try:
                obj = json.loads(obj)
            except ValueError:
                return
        stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        stdout.flush()

    def note_protocol(text: str) -> None:
        nonlocal protocol
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if isinstance(msg, dict) and isinstance(msg.get("result"), dict) and msg["result"].get("protocolVersion"):
            protocol = str(msg["result"]["protocolVersion"])

    with httpx.Client(timeout=httpx.Timeout(600, connect=15), transport=transport) as http:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            ids = _ids(line)
            try:
                h = {"content-type": "application/json", "accept": ACCEPT, **headers()}
            except Exception as e:  # e.g. the DataOS login expired: tell the agent why, keep running
                for rid in ids:
                    emit(_error(rid, str(e)))
                continue
            if session:
                h["mcp-session-id"] = session
            if protocol:
                h["mcp-protocol-version"] = protocol
            try:
                with http.stream("POST", url, content=line.encode(), headers=h) as r:
                    session = r.headers.get("mcp-session-id", session)
                    if r.status_code == 202:
                        continue
                    ctype = r.headers.get("content-type", "")
                    if r.status_code >= 400:
                        body = r.read().decode(errors="replace")
                        try:
                            detail = json.loads(body)["error"]["message"]
                        except (ValueError, KeyError, TypeError):
                            detail = body[:300]
                        if r.status_code == 404 and session and "mcp-session-id" in h:
                            session = None  # the tool restarted and forgot the session: the client re-initializes
                        for rid in ids:
                            emit(_error(rid, f"HTTP {r.status_code}: {detail}"))
                        continue
                    if "text/event-stream" in ctype:
                        for msg in _sse_messages(r.iter_lines()):
                            note_protocol(msg)
                            emit(msg)
                    else:
                        body = r.read().decode()
                        if body.strip():
                            note_protocol(body)
                            emit(body.strip())
            except httpx.HTTPError as e:
                for rid in ids:
                    emit(_error(rid, f"gateway unreachable: {e.__class__.__name__}"))
