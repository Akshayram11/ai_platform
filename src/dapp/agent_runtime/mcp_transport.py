"""Streamable HTTP sessions, using the gateway client from the Log Assistant."""

from __future__ import annotations

import json
import os
import threading
import time

import httpx

RETRY_SECONDS = float(os.environ.get("MCP_RETRY_SECONDS", "45"))
_sleep = time.sleep        # replaced in tests


class ToolError(RuntimeError):
    pass


def _messages(resp: httpx.Response) -> list[dict]:
    """A JSON-RPC reply comes as JSON or as an SSE stream of `data:` lines."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        out = []
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except ValueError:
                    pass
        return out
    body = resp.json() if resp.content.strip() else []
    return body if isinstance(body, list) else [body]


class Server:
    """One MCP server behind the gateway. A session is opened lazily and reopened if the server forgot it."""

    def __init__(self, name: str, url: str, key: str, http: httpx.Client, retry_seconds: float | None = None):
        self.name, self.url, self.key, self.http = name, url, key, http
        self.retry_seconds = RETRY_SECONDS if retry_seconds is None else retry_seconds   # a quick check waits less
        self.session: str | None = None
        self.opened = False        # stateless servers send no session id: remember that we initialized anyway
        self.protocol = "2025-06-18"
        self.instructions = ""
        self._id = 0
        self._lock = threading.Lock()

    def _post(self, payload: dict, user: str) -> httpx.Response:
        h = {"authorization": f"Bearer {self.key}", "content-type": "application/json",
             "accept": "application/json, text/event-stream", "mcp-protocol-version": self.protocol}
        if self.session:
            h["mcp-session-id"] = self.session
        if user:
            h["x-dapp-user"] = user
        if payload.get("method") == "tools/call":
            # An upstream error can arrive after a tool has changed state.
            return self.http.post(self.url, json=payload, headers=h)
        deadline, delay = time.monotonic() + self.retry_seconds, 1.0
        while True:
            try:
                r = self.http.post(self.url, json=payload, headers=h)
                if r.status_code not in (502, 503, 504) or time.monotonic() + delay >= deadline:
                    return r
            except (httpx.ConnectError, httpx.ConnectTimeout):      # nothing reached the gateway
                if time.monotonic() + delay >= deadline:
                    raise
            _sleep(delay)
            delay = min(delay + 1.0, 5.0)

    def _open(self, user: str) -> None:
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "dapp-agent", "version": "1"}}}, user)
        if r.status_code >= 400:
            raise ToolError(f"{self.name}: HTTP {r.status_code} {r.text[:200]}")
        self.session = r.headers.get("mcp-session-id")
        result = next((m.get("result") for m in _messages(r) if "result" in m), {}) or {}
        self.protocol = result.get("protocolVersion", self.protocol)
        self.instructions = (result.get("instructions") or "")[:4000]
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, user)
        self.opened = True

    def call(self, method: str, params: dict, user: str) -> dict:
        with self._lock:
            for attempt in (1, 2):
                if not self.opened:
                    self._open(user)
                self._id += 1
                r = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, user)
                if r.status_code == 404 and self.session and attempt == 1 and method != "tools/call":
                    self.session, self.opened = None, False  # server restarted, session lost: reopen once
                    continue
                if r.status_code >= 400:
                    raise ToolError(f"{self.name}: HTTP {r.status_code} {r.text[:300]}")
                for m in _messages(r):
                    if m.get("id") == self._id:
                        if "error" in m:
                            raise ToolError(f"{self.name}: {m['error'].get('message', m['error'])}")
                        return m.get("result") or {}
                raise ToolError(f"{self.name}: no reply to {method}")
        raise ToolError(f"{self.name}: session could not be re-opened")
