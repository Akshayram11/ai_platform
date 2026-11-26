"""Log Assistant: a DataOS-style Logs page with an AI button that opens a chat about those logs, on the same page.

Everything comes from the gateway tool `dapp-monitor` (apps and services: status, pods, recent logs, stored logs over any
time range, error counts): the page shows its data, and the model (OpenAI `gpt-6-luna` through the dapp gateway, metered per
person) calls the same tools itself to answer. The chat is told what the page shows (which resource, which window, which
keyword), so "is anything wrong here?" means this resource, this window.

Needs (dapp.yaml): llm: true, tools: [dapp-monitor]; and on the gateway `dapp tool allow dapp-monitor app:log-assistant`.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from mcp_tools import Toolbox, ToolError
from openai import OpenAI
from pydantic import BaseModel, Field

log = logging.getLogger("log-assistant")
ROOT = os.environ.get("ROOT_PATH", "")
MODEL = os.environ.get("LOG_MODEL", "gpt-6-luna")
MONITOR = os.environ.get("MONITOR_TOOL", "dapp-monitor")
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "8"))
MAX_TURNS = 12
PAGE = (Path(__file__).parent / "index.html").read_text()

app = FastAPI(title="Log Assistant", root_path=ROOT)
TOOLS = Toolbox.from_env()
_client: OpenAI | None = None

SYSTEM = """You are the Log Assistant of DataOS. You help a person understand the logs of DataOS resources that run pods: apps,
services, vulcan data products, minerva clusters, lakehouses, nilus pipelines, workflows and workers (pass kind).
Use the dapp-monitor tools to look; never guess what a log says. Prefer, in order: log_summary (where to look), search_logs
(a keyword and a window: since='1h' or start/end in ISO UTC), recent_logs (what is happening right now), status (is it up,
which pods). Times in the logs are UTC; say times in UTC. Quote the few log lines that support your answer (as `code`), say
which pod they came from, and say plainly when nothing was found. Keep answers short. Answer in the language the person
writes in (Hinglish if they write Hinglish). Credentials in logs are masked (•••): never try to recover them."""


# ------------------------------------------------------------------ the monitor tool, for the page
def who(request: Request) -> str:
    return request.headers.get("x-dapp-user", "") or "anonymous"


def monitor(fn: str, user: str, **args: Any) -> Any:
    """Call one dapp-monitor function through the gateway and return its result (or a 502 with the reason)."""
    if TOOLS is None or MONITOR not in TOOLS.servers:
        raise HTTPException(503, f"the {MONITOR} tool is not configured (tools: [{MONITOR}] and llm: true in dapp.yaml)")
    try:
        res = TOOLS.servers[MONITOR].call("tools/call", {"name": fn, "arguments": {k: v for k, v in args.items()
                                                                                      if v not in (None, "")}}, user)
    except ToolError as e:
        raise HTTPException(502, str(e)) from e
    text = "\n".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
    if res.get("isError"):
        raise HTTPException(502, text or f"{fn} failed")
    data = res.get("structuredContent")
    if data is None:
        try:
            data = json.loads(text)
        except ValueError:
            return text
    return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data


@app.get("/", response_class=HTMLResponse)
def page() -> str:
    return PAGE.replace("__ROOT__", ROOT)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL, "tool": MONITOR, "tools_configured": TOOLS is not None}


@app.get("/api/resources")
def resources(request: Request, kind: str = "app") -> Any:
    return monitor("list_resources", who(request), kind=kind)


@app.get("/api/status")
def status(request: Request, name: str, kind: str = "app") -> Any:
    return monitor("status", who(request), name=name, kind=kind)


@app.get("/api/logs")
def logs(request: Request, name: str, kind: str = "app", mode: Literal["live", "historical"] = "historical",
         q: str = "*", since: str | None = "1h", start: str | None = None, end: str | None = None,
         pod: str | None = None, limit: int = 300) -> dict:
    """The page's log table: the running pods' last lines (live), or stored logs of a window (historical), one shape."""
    user = who(request)
    if mode == "live":
        r = monitor("recent_logs", user, name=name, kind=kind, tail=min(max(limit, 1), 1000), pod=pod)
        lines = [{"time": None, "pod": c["pod"], "container": c["container"], "stream": None, "message": m}
                 for c in r.get("containers") or [] for m in c.get("lines") or []]
        return {"mode": "live", "total": len(lines), "returned": len(lines), "lines": lines, "container": None}
    window = {"start": start, "end": end} if (start or end) else {"since": since or "1h"}
    r = monitor("search_logs", user, name=name, kind=kind, query=q or "*", pod=pod, limit=min(max(limit, 1), 1000), **window)
    return {"mode": "historical", **{k: r.get(k) for k in ("total", "returned", "lines", "container", "query")}}


# ------------------------------------------------------------------ the chat
class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class PageContext(BaseModel):
    name: str | None = None
    kind: str = "app"
    mode: str = "historical"
    q: str | None = None
    since: str | None = None
    start: str | None = None
    end: str | None = None
    pod: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    context: PageContext = PageContext()


def context_note(c: PageContext) -> str:
    if not c.name:
        return "The page shows no resource yet."
    window = f"from {c.start or '…'} to {c.end or 'now'} (UTC)" if (c.start or c.end) else f"the last {c.since or '1h'}"
    return (f"The page now shows the {c.mode} logs of {c.kind} '{c.name}'"
            + (f", pod {c.pod}" if c.pod else "") + (f", filtered by '{c.q}'" if c.q and c.q != "*" else "")
            + f", {window}. Unless the person says otherwise, questions are about this {c.kind} and this window.")


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> dict:
    global _client
    if TOOLS is None:
        raise HTTPException(503, "tools are not configured")
    user = who(request)
    specs = [s for s in TOOLS.openai_tools(user) if s["function"]["name"].startswith(MONITOR.replace("-", "_"))]
    if not specs:
        raise HTTPException(502, f"the {MONITOR} tool did not answer: {'; '.join(TOOLS.problems) or 'no functions'}")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    system = f"{SYSTEM}\n\nNow it is {now}. {context_note(req.context)}"
    if TOOLS.instructions():
        system += "\n\nThe tool's own guidance:\n" + TOOLS.instructions()
    messages: list[dict] = [{"role": "system", "content": system},
                            *[m.model_dump() for m in req.messages][-MAX_TURNS:]]
    _client = _client or OpenAI(api_key=os.environ.get("DAPP_LLM_KEY") or None)
    # who is asking, for the gateway's usage per person; chat completions takes function tools only without reasoning
    extra: dict[str, Any] = {"extra_body": {"reasoning_effort": "none"}}
    if user != "anonymous" and os.environ.get("DAPP_LLM_KEY"):
        extra["extra_headers"] = {"x-dapp-user": user}
    steps: list[dict] = []
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = _client.chat.completions.create(model=MODEL, messages=messages, tools=specs, **extra)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return {"answer": msg.content or "", "steps": steps, "model": MODEL}
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                result = TOOLS.run(tc.function.name, tc.function.arguments or "{}", user)
                steps.append({"tool": tc.function.name.split("__", 1)[-1], "args": _args(tc.function.arguments),
                              "result": _gist(result)})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    except Exception as e:                       # say it in the chat rather than as a broken request
        log.exception("chat failed")
        return {"answer": f"[error: {type(e).__name__}: {e}]", "steps": steps, "model": MODEL}
    return {"answer": "[stopped: too many tool calls for one question; ask something narrower]", "steps": steps, "model": MODEL}


def _args(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _gist(result: str) -> str:
    """One line about a tool result, for the chat's "what I looked at" list."""
    try:
        d = json.loads(result)
    except ValueError:                  # a long result is cut by the toolbox, so not valid JSON any more: read the counts
        nums = {k: re.search(rf'"{k}":\s*(\d+)', result) for k in ("total", "returned", "total_lines")}
        if nums["total"]:
            shown = f", showing {nums['returned'].group(1)}" if nums["returned"] and nums["returned"].group(1) != nums["total"].group(1) else ""
            return f"{nums['total'].group(1)} matching lines{shown}"
        if nums["total_lines"]:
            return f"{nums['total_lines'].group(1)} lines"
        return " ".join(result.split())[:160]
    d = d.get("result", d) if isinstance(d, dict) else d
    if isinstance(d, dict):
        if "total" in d and "lines" in d:
            return f"{d['total']} matching lines" + (f", showing {d.get('returned')}" if d.get("returned") != d["total"] else "")
        if "matching" in d:
            m = d["matching"]
            return f"{d.get('total_lines')} lines: {m.get('error')} error, {m.get('warn')} warn, {m.get('exception')} exception"
        if "containers" in d:
            return f"{sum(len(c.get('lines') or []) for c in d['containers'])} recent lines"
        if "state" in d:
            return f"{d.get('state')} · {d.get('runtime') or ''} · {len(d.get('pods') or [])} pods"
    if isinstance(d, list):
        return f"{len(d)} items"
    return str(d)[:160]
