"""MCP tools for the chatbot, through the dapp gateway.

dapp sets, for apps deployed with `tools: [...]` in dapp.yaml:
  DAPP_TOOLS       comma list of gateway tool names, e.g. dataproduct-mcp
  DAPP_TOOLS_URL   http://dapp-gateway:8080/mcp/<tenant>
  DAPP_LLM_KEY     the app's gateway key, which also proves the app to the gateway for tools

The app never holds a tool's credential: the gateway checks that this app may use the tool, adds the tool's own
credential and records the call (with the signed-in person we pass in x-dapp-user).
"""

from __future__ import annotations

import json
import logging
import os
import threading

import httpx

log = logging.getLogger("chatbot.tools")
SEP = "__"                 # OpenAI function name = <tool>__<function>; both parts are [A-Za-z0-9_-]
MAX_RESULT = 12_000        # characters of a tool result handed back to the model


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

    def __init__(self, name: str, url: str, key: str, http: httpx.Client):
        self.name, self.url, self.key, self.http = name, url, key, http
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
        return self.http.post(self.url, json=payload, headers=h)

    def _open(self, user: str) -> None:
        self._id += 1
        r = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize", "params": {
            "protocolVersion": self.protocol, "capabilities": {},
            "clientInfo": {"name": "dapp-chatbot", "version": "1"}}}, user)
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
                if r.status_code == 404 and self.session and attempt == 1:
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


class Toolbox:
    def __init__(self, names: list[str], base: str, key: str, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(timeout=httpx.Timeout(120, connect=10), transport=transport)
        self.servers = {n: Server(n, f"{base.rstrip('/')}/{n}", key, self.http) for n in names}
        self._specs: list[dict] | None = None
        self.problems: list[str] = []

    @classmethod
    def from_env(cls) -> Toolbox | None:
        names = [n.strip() for n in os.environ.get("DAPP_TOOLS", "").split(",") if n.strip()]
        base, key = os.environ.get("DAPP_TOOLS_URL", ""), os.environ.get("DAPP_LLM_KEY", "")
        return cls(names, base, key) if names and base and key else None

    def openai_tools(self, user: str) -> list[dict]:
        """Every function of every server, as OpenAI tool definitions. Listed once, then cached."""
        if self._specs is None:
            specs, self.problems = [], []
            for name, srv in self.servers.items():
                try:
                    for t in srv.call("tools/list", {}, user).get("tools", []):
                        specs.append({"type": "function", "function": {
                            "name": f"{name.replace('-', '_')}{SEP}{t['name']}"[:64],
                            "description": (t.get("description") or "")[:1024],
                            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})
                except Exception as e:
                    self.problems.append(str(e))
                    log.warning("could not list tools of %s: %s", name, e)
            if specs:  # don't cache a total failure: try again on the next message
                self._specs = specs
            return specs
        return self._specs

    def instructions(self) -> str:
        return "\n\n".join(f"[{n}]\n{s.instructions}" for n, s in self.servers.items() if s.instructions)

    def run(self, fn_name: str, arguments: str, user: str) -> str:
        server_part, _, fn = fn_name.partition(SEP)
        srv = next((s for n, s in self.servers.items() if n.replace("-", "_") == server_part), None)
        if srv is None or not fn:
            return f"unknown tool {fn_name}"
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return "the arguments were not valid JSON"
        try:
            res = srv.call("tools/call", {"name": fn, "arguments": args}, user)
        except ToolError as e:
            return f"tool error: {e}"
        parts = []
        for c in res.get("content") or []:
            parts.append(c.get("text") if c.get("type") == "text" else json.dumps(c)[:2000])
        if not parts and res.get("structuredContent") is not None:
            parts.append(json.dumps(res["structuredContent"]))
        text = "\n".join(p for p in parts if p) or "(empty result)"
        if res.get("isError"):
            text = "tool reported an error: " + text
        return text if len(text) <= MAX_RESULT else text[:MAX_RESULT] + f"\n…[cut: {len(text)} characters in all]"
