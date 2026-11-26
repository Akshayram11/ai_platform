"""A small OpenAI chatbot to show dapp end to end.

It only knows the standard OpenAI env vars, so it runs unchanged in two setups:
  - llm: true   -> dapp points OPENAI_BASE_URL at the dapp gateway and gives it one per-app key, DAPP_LLM_KEY;
                   the real OpenAI key lives only in the gateway, and tokens show in the dashboard.
  - llm: false  -> put OPENAI_API_KEY in a DataOS secret and the app calls OpenAI directly.
With `tools: [...]` in dapp.yaml the bot can also call MCP tools through the gateway (see mcp_tools.py): the model
decides when, the app runs the call, the gateway checks access, adds the tool's credential and records it.
Local:  OPENAI_API_KEY=... uvicorn app:app --port 8080
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from mcp_tools import Toolbox
from openai import OpenAI
from pydantic import BaseModel

try:
    from dapp_objects import (  # installed in the dapp runtime; copied in by our Dockerfile
        ObjectsError,
        objects,
    )
except ImportError:  # local run without the helper: the chat works, it just isn't saved
    objects = None
    ObjectsError = RuntimeError

log = logging.getLogger("chatbot")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
# Models the user may switch to in the page (the first is MODEL). Others, e.g. "small" (served inside DataOS by the
# gateway), get a plain chat: no reasoning setting and no tools, which small models don't handle well.
# OPENAI_MODELS names some up front; the rest come live from the gateway: every model deployed in the dashboard's Models tab
# shows up in the drop-down within seconds, marked "starting" until its server has loaded it.
STATIC_MODELS = [m.strip() for m in os.environ.get("OPENAI_MODELS", "").split(",") if m.strip() and m.strip() != MODEL]
GATEWAY = os.environ.get("DAPP_LLM_GATEWAY_URL", "").rstrip("/")
_local: dict = {"at": -1e9, "models": []}
SYSTEM = os.environ.get("SYSTEM_PROMPT", "You are a helpful, concise assistant running on DataOS.")
REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "")  # reasoning models only: none|low|medium|high
MAX_TURNS = 20
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "12"))
TOOLS = Toolbox.from_env()
# Chat history lives in immortal-obj (`objects: true` in dapp.yaml): one object "chat" per signed-in person, kept by
# the app, shared back to that person (read-only) so they can open it in the dashboard's Storage page.
HISTORY = bool(objects and os.environ.get("DAPP_OBJECTS_URL"))
_ready: set[str] = set()

app = FastAPI(title="dapp chatbot", root_path=os.environ.get("ROOT_PATH", ""))
_client: OpenAI | None = None
PAGE = (Path(__file__).parent / "index.html").read_text()


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]
    model: str | None = None
    chat_id: str | None = None      # the saved conversation this belongs to; none = start a new one


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE.replace("{{MODEL}}", MODEL)


def local_models() -> list[dict]:
    """Models served inside DataOS that this app's gateway key can use (name, repo, ready), cached for a few seconds.
    If the gateway can't be asked, the last answer stays."""
    key = os.environ.get("DAPP_LLM_KEY", "")
    if GATEWAY and key and time.monotonic() - _local["at"] > 8:
        try:
            r = httpx.get(f"{GATEWAY}/local-models", headers={"authorization": f"Bearer {key}"}, timeout=3)
            if r.status_code == 200:
                _local["models"] = r.json().get("models", [])
        except (httpx.HTTPError, ValueError) as e:
            log.info("could not list the local models: %s", e)
        _local["at"] = time.monotonic()
    return _local["models"]


def model_status() -> dict[str, dict]:
    """Every model the page offers: the default first, then the static ones, then what the gateway lists. status has the
    ready flag of the local ones."""
    local = {m["name"]: m for m in local_models()}
    names = [MODEL, *[m for m in STATIC_MODELS if m not in (MODEL,)], *[n for n in local if n != MODEL and n not in STATIC_MODELS]]
    return {n: ({"ready": bool(local[n].get("ready")), "repo": local[n].get("repo", ""), "local": True} if n in local
                else {"ready": True, "repo": "", "local": n != MODEL}) for n in names}


@app.get("/api/models")
def models() -> dict:
    status = model_status()
    return {"default": MODEL, "models": list(status), "status": status}


def who(request: Request) -> str:
    return request.headers.get("x-dapp-user", "")


def history(user: str):
    """This person's chat object, created on first use. Tables: chats (id, title, updated), msgs (chat_id, role, ...)."""
    db = objects.open("chat", for_user=user or None)
    if user not in _ready:
        db.sql("create table if not exists chats (id text primary key, title text, "
               "created text default current_timestamp, updated text default current_timestamp)")
        db.sql("create table if not exists msgs (id integer primary key autoincrement, chat_id text, role text, "
               "content text, model text, at text default current_timestamp)")
        if user:
            try:
                db.share(user)           # theirs to read in the dashboard (Storage); only the app can write it
            except ObjectsError as e:
                log.info("not shared back to %s: %s", user, e)
        _ready.add(user)
    return db


def start_turn(user: str, chat_id: str | None, text: str) -> str | None:
    """Save the question; returns the conversation id, or None when saving isn't possible (the chat still works)."""
    try:
        db = history(user)
        if chat_id and not db.sql("select 1 as x from chats where id = ?", [chat_id]):
            chat_id = None
        if not chat_id:
            chat_id = uuid.uuid4().hex[:12]
            db.sql("insert into chats (id, title) values (?, ?)", [chat_id, " ".join(text.split())[:60] or "New chat"])
        db.sql("insert into msgs (chat_id, role, content) values (?, 'user', ?)", [chat_id, text])
        return chat_id
    except (ObjectsError, OSError) as e:
        log.warning("chat history not saved: %s", e)
        return None


def finish_turn(user: str, chat_id: str, model: str, reply: str) -> None:
    if not reply.strip() or reply.lstrip().startswith("[error"):
        return
    try:
        db = history(user)
        db.sql("insert into msgs (chat_id, role, content, model) values (?, 'assistant', ?, ?)", [chat_id, reply, model])
        db.sql("update chats set updated = current_timestamp where id = ?", [chat_id])
    except (ObjectsError, OSError) as e:
        log.warning("reply not saved: %s", e)


@app.get("/api/history")
def list_history(request: Request) -> dict:
    if not HISTORY:
        return {"enabled": False, "chats": []}
    try:
        rows = history(who(request)).sql("select id, title, updated from chats order by updated desc, rowid desc limit 50")
    except (ObjectsError, OSError) as e:
        return {"enabled": False, "chats": [], "error": str(e)}
    return {"enabled": True, "chats": rows}


@app.get("/api/history/{chat_id}")
def get_history(chat_id: str, request: Request) -> dict:
    if not HISTORY:
        raise HTTPException(404, "chat history is off: add `objects: true` to dapp.yaml")
    rows = history(who(request)).sql("select role, content, model from msgs where chat_id = ? order by id", [chat_id])
    return {"id": chat_id, "messages": rows}


@app.delete("/api/history/{chat_id}")
def delete_history(chat_id: str, request: Request) -> dict:
    if not HISTORY:
        raise HTTPException(404, "chat history is off: add `objects: true` to dapp.yaml")
    db = history(who(request))
    db.sql("delete from msgs where chat_id = ?", [chat_id])
    db.sql("delete from chats where id = ?", [chat_id])
    return {"deleted": chat_id}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL, "models": list(model_status()), "via_gateway": "dapp" in os.environ.get("OPENAI_BASE_URL", ""),
            "tools": list(TOOLS.servers) if TOOLS else [], "history": HISTORY}


def gateway_user_headers(request: Request) -> dict[str, str]:
    """Tell the dapp gateway who is chatting, so the dashboard can show usage per person. dapp_login puts the
    signed-in DataOS user in x-dapp-user. Only sent through the gateway: never to OpenAI itself."""
    user = request.headers.get("x-dapp-user", "")
    return {"x-dapp-user": user} if user and os.environ.get("DAPP_LLM_KEY") else {}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    model = req.model or MODEL
    status = model_status()
    if model not in status:
        raise HTTPException(400, f"model must be one of {', '.join(status)}")
    main = model == MODEL
    user_headers = gateway_user_headers(request)
    turns = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")][-MAX_TURNS:]
    user = who(request)
    chat_id = None
    if HISTORY and req.messages and req.messages[-1].role == "user":
        chat_id = start_turn(user, req.chat_id, req.messages[-1].content)

    def stream():
        parts: list[str] = []
        try:
            for piece in produce():
                parts.append(piece)
                yield piece
        finally:                        # also when the browser went away mid-answer: keep what was said
            if chat_id:
                finish_turn(user, chat_id, model, "".join(parts))

    def produce():
        global _client
        if not status[model]["ready"]:
            yield (f"[{model} is still starting: its server is downloading and loading the model. "
                   "Try again in a minute, or pick another model.]")
            return
        try:
            # DAPP_LLM_KEY (gateway key) when deployed with llm: true; otherwise OPENAI_API_KEY from the environment
            _client = _client or OpenAI(api_key=os.environ.get("DAPP_LLM_KEY") or None)
            extra = {"extra_body": {"reasoning_effort": REASONING_EFFORT}} if REASONING_EFFORT and main else {}
            if user_headers:
                extra["extra_headers"] = user_headers
            if TOOLS and main:
                yield from with_tools(_client, turns, extra, user)
                return
            for chunk in _client.chat.completions.create(
                model=model, messages=[{"role": "system", "content": SYSTEM}, *turns], stream=True, **extra
            ):
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:  # show the reason in the chat instead of a dead stream
            yield f"\n\n[error: {type(e).__name__}: {e}]"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8",
                             headers={"X-Chat-Id": chat_id} if chat_id else {})


def with_tools(client: OpenAI, turns: list[dict], extra: dict, user: str):
    """The model asks for tools, we run them through the gateway, until it answers. Progress lines are streamed
    so a slow data query doesn't look like a hang."""
    specs = TOOLS.openai_tools(user)
    if not specs:
        yield f"[tools unavailable: {'; '.join(TOOLS.problems) or 'no tools listed'}]\n\n"
    system = SYSTEM
    if TOOLS.instructions():
        system += "\n\nYou can use these tools. Their own guidance:\n" + TOOLS.instructions()
    messages: list[dict] = [{"role": "system", "content": system}, *turns]
    if specs and "extra_body" in extra:  # chat completions refuses function tools with reasoning on (gpt-6-luna)
        extra = {**extra, "extra_body": {**extra["extra_body"], "reasoning_effort": "none"}}
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.chat.completions.create(model=MODEL, messages=messages, tools=specs or None, **extra)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            yield msg.content or ""
            return
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
        for tc in msg.tool_calls:
            label = tc.function.name.split("__", 1)[-1]
            args = tc.function.arguments or ""
            yield f"› {label}({args[:120]}{'…' if len(args) > 120 else ''})\n"
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": TOOLS.run(tc.function.name, args, user)})
    yield "\n[stopped: too many tool calls for one question; ask something narrower]"


if os.environ.get("DAPP_AUTH") == "oidc":  # set by dapp when the app is private (the default)
    from dapp_login import protect

    app = protect(app)

if os.environ.get("DAPP_METRICS", "1") == "1":  # CPU/memory for the dapp dashboard, on an internal port
    import dapp_metrics

    dapp_metrics.start()
    app = dapp_metrics.instrument(app)
