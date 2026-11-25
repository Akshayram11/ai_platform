"""Data Chat: ask about DataOS data products in plain words; the small in-DataOS model explains the result.

"small" (0.5B) can't do function calling and has a 4096-token context, so the app does the tool work itself and only asks the
model to put the numbers into words:

  1. search the catalog for data products that fit the question      (MCP tool: search)
  2. read the best one's semantic layer (its measures and dimensions) (vulcan_semantic_schema)
  3. match the question to a measure and a dimension by words
  4. run the query and wait for it                                    (vulcan_query_submit, vulcan_query_result)
  5. hand the trimmed result to the model to explain

Every step is streamed into the chat ("› ..."). If the data can't be reached the chat says so. History is kept per signed-in
person in immortal-obj (`objects: true`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from mcp_tools import Toolbox
from openai import OpenAI
from pydantic import BaseModel

try:
    from dapp_objects import ObjectsError, objects
except ImportError:  # run locally without the helper: the chat works, it just isn't saved
    objects = None
    ObjectsError = RuntimeError

log = logging.getLogger("data-chat")
MODEL = os.environ.get("DATA_MODEL", "small")
TOOLS_URL = os.environ.get("DAPP_TOOLS_URL", "")
TENANT = os.environ.get("DATA_TENANT") or TOOLS_URL.rstrip("/").rsplit("/", 1)[-1] or os.environ.get("DATAOS_TENANT_ID", "")
BOX = Toolbox.from_env()
HISTORY = bool(objects and os.environ.get("DAPP_OBJECTS_URL"))
MAX_ROWS = 20                 # rows of a result handed to the model
MAX_RESULT_CHARS = 5000       # about 1700 tokens of the model's 4096
POLL_SECONDS, POLL_TRIES = 3, 25
BAD_REPLY = ("tool error", "tool reported an error", "unknown tool", "the arguments were not valid JSON")
STOP = {"the", "a", "an", "of", "in", "on", "for", "and", "to", "is", "are", "was", "were", "what", "how", "show", "me",
        "give", "with", "from", "all", "my", "our", "please", "tell", "about", "which", "who", "whats", "do", "does", "did",
        "by", "per", "each", "there", "here", "this", "that", "it", "we", "have", "has", "i", "you", "your", "can", "could",
        "get", "see", "know"}

app = FastAPI(title="data-chat", root_path=os.environ.get("ROOT_PATH", ""))
PAGE = (Path(__file__).parent / "index.html").read_text()
_client: OpenAI | None = None
_ready: set[str] = set()


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]
    chat_id: str | None = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE.replace("{{MODEL}}", MODEL)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL, "tenant": TENANT, "mcp": bool(BOX), "history": HISTORY}


# ------------------------------------------------------------------ the data side
def call(short: str, args: dict, user: str) -> tuple[bool, str]:
    """One Data Product MCP function through the gateway: (worked, text)."""
    text = BOX.run(f"dataproduct_mcp__{short}", json.dumps(args), user)
    return (not text.startswith(BAD_REPLY)), text


def find_products(text: str) -> list[tuple[str, str]]:
    """(title, data product name) pairs from a catalog search result."""
    return re.findall(r"^\d+\.\s+(.+?)\n\s+arn:\s*arn:[^/\s]+/(\S+)", text, flags=re.MULTILINE)


def words(text: str) -> list[str]:
    return [norm(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


def norm(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def parse_schema(markdown: str) -> dict[str, list[dict]]:
    """Measures and dimensions of a semantic schema as {"measures": [...], "dimensions": [...]} with fully-qualified names."""
    out: dict[str, list[dict]] = {"measures": [], "dimensions": []}
    for block in re.split(r"\n## ", "\n" + markdown)[1:]:
        model = block.splitlines()[0].strip()
        for kind in ("Measures", "Dimensions"):
            part = re.search(rf"### {kind}\n(.*?)(?=\n### |\Z)", block, flags=re.DOTALL)
            if not part:
                continue
            rows = [ln for ln in part.group(1).splitlines() if ln.startswith("|")]
            if len(rows) < 3:
                continue
            head = [c.strip().lower() for c in rows[0].strip("|").split("|")]
            col = {n: head.index(n) for n in ("name", "description", "synonyms") if n in head}
            for ln in rows[2:]:
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                if "name" not in col or len(cells) <= col["name"]:
                    continue
                row = {k: (cells[i] if len(cells) > i else "") for k, i in col.items()}
                out[kind.lower()].append({"fq": f"{model}.{row['name']}", "name": row["name"],
                                          "desc": row.get("description", ""), "syn": row.get("synonyms", "")})
    return out


def score(item: dict, toks: list[str]) -> int:
    name = {norm(w) for w in re.split(r"[^a-z0-9]+", item["name"].lower()) if w}
    text = {norm(w) for w in re.findall(r"[a-z0-9]+", f"{item['desc']} {item['syn']}".lower())}
    return sum(3 for t in toks if t in name) + sum(1 for t in toks if t in text)


FILLER = {"many", "much", "number", "total", "average", "avg", "sum", "count", "amount", "top"}   # say how, not what
MIN_COVERAGE = 0.6   # share of the question's content words the measure or dimension must account for


def covered(item: dict, toks: list[str]) -> float:
    name = {norm(w) for w in re.split(r"[^a-z0-9]+", item["name"].lower()) if w}
    text = {norm(w) for w in re.findall(r"[a-z0-9]+", f"{item['desc']} {item['syn']}".lower())}
    content = [t for t in toks if t not in FILLER]
    return sum(t in name or t in text for t in content) / len(content) if content else 1.0


def pick(items: list[dict], toks: list[str]) -> dict | None:
    """The item that fits the words best, only when it explains most of them: a weak match would answer another question."""
    best = max(items, key=lambda i: (score(i, toks), -len(i["name"])), default=None)
    return best if best and score(best, toks) > 0 and covered(best, toks) >= MIN_COVERAGE else None


def plan(question: str, catalog: dict[str, list[dict]]) -> tuple[dict | None, dict | None]:
    """A measure and maybe a dimension that fit the question: words before 'by'/'per' pick the measure, after it the dimension."""
    parts = re.split(r"\b(?:by|per|for each|grouped by|broken down by)\b", question.lower(), maxsplit=1)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    measure = pick(catalog["measures"], words(head)) or pick(catalog["measures"], words(question))
    dim = pick(catalog["dimensions"], words(tail)) if tail.strip() else None
    return measure, dim


def run_query(dp: str, measure: dict, dim: dict | None, user: str):
    """Submit, wait, yield (step text) and finally ("result", json) or ("error", text)."""
    args = {"dp_name": dp, "tenant": TENANT, "measures": [measure["fq"]], "limit": MAX_ROWS}
    if dim:
        args["dimensions"] = [dim["fq"]]
    yield "step", f"vulcan_query_submit(measures={args['measures']}{', dimensions=' + str(args['dimensions']) if dim else ''})"
    ok, text = call("vulcan_query_submit", args, user)
    try:
        sid = json.loads(text).get("statement_id") if ok else None
    except ValueError:
        sid = None
    if not sid:
        yield "error", text[:400]
        return
    for i in range(POLL_TRIES):
        time.sleep(POLL_SECONDS)
        ok, text = call("vulcan_query_result", {"statement_id": sid, "dp_name": dp, "tenant": TENANT}, user)
        try:
            data = json.loads(text)
        except ValueError:
            yield "error", text[:400]
            return
        status = str(data.get("status", "")).upper()
        if status == "SUCCESS":
            rows = (data.get("results") or [])[:MAX_ROWS]
            yield "step", f"vulcan_query_result → {data.get('row_count', len(rows))} row(s)"
            yield "result", json.dumps(rows, default=str)[:MAX_RESULT_CHARS]
            return
        if status in ("FAILED", "ERROR", "CANCELLED") or data.get("success") is False:
            yield "error", (data.get("error") or data.get("message") or text)[:400]
            return
        if i == 0:
            yield "step", "waiting for the query to finish…"
    yield "error", "the query did not finish in time"


def explain(question: str, facts: str) -> list[dict]:
    return [{"role": "system", "content": "You explain data results to a business reader in plain words. Use only the facts "
                                          "given: never invent numbers. If the facts say no number was computed or the query failed, say that plainly and "
                                          "do not guess an answer. Answer in 2 to 4 short sentences."},
            {"role": "user", "content": f"Question: {question}\n\n{facts}\n\nAnswer the question from these facts."}]


def answer(question: str, user: str):
    """Everything the chat says for one question, as text chunks. Lines starting with '›' are steps."""
    if BOX is None or not TENANT:
        yield "[The Data Product MCP isn't set up for this app: deploy it with `tools: [dataproduct-mcp]`.]"
        return
    explicit = re.search(r"\bdp:([a-z0-9][a-z0-9-]*)", question)
    question = re.sub(r"\bdp:[a-z0-9-]+\s*", "", question).strip() or question
    if explicit:
        candidates = [("", explicit.group(1))]
    else:
        yield f"› search(index=data_product, tenant={TENANT})\n"
        for q in (question[:200], ""):
            args = {"index": "data_product", "tenant": TENANT, "limit": 5, **({"q": q} if q else {})}
            ok, text = call("search", args, user)
            if not ok:
                yield f"\n[I couldn't reach the data: {text[:300]}]"
                return
            candidates = find_products(text)
            if candidates:
                break
        if not candidates:
            yield "\n[No data products were found in the catalog for this tenant.]"
            return
        yield f"› found {len(candidates)} data product(s): {', '.join(d for _, d in candidates)} (say dp:<name> to pick one)\n"
    # the first data product whose measures fit the question and whose query works wins
    facts, first, failures = None, None, []
    for i, (title, dp) in enumerate(candidates[:3]):
        yield f"› vulcan_semantic_schema({dp})\n"
        ok, text = call("vulcan_semantic_schema", {"dp_name": dp, "tenant": TENANT}, user)
        if not ok:
            yield f"[I couldn't read {dp}: {text[:300]}]\n"
            continue
        try:
            schema = json.loads(text).get("schema", text)
        except ValueError:
            schema = text
        catalog = parse_schema(schema)
        if first is None or (not first[2]["measures"] and catalog["measures"]):   # describe one that has something to ask
            first = (title or dp, dp, catalog)
        measure, dim = plan(question, catalog)
        if not measure:
            if i + 1 < min(len(candidates), 3):
                yield f"› nothing in {dp} fits the question, trying the next one\n"
            continue
        for kind, payload in run_query(dp, measure, dim, user):
            if kind == "step":
                yield f"› {payload}\n"
            elif kind == "result":
                facts = (f"Data product: {title or dp}\nMeasure: {measure['name']} ({measure['desc'] or 'no description'})"
                         + (f"\nBroken down by: {dim['name']}" if dim else "") + f"\nResult rows (JSON): {payload}")
            else:
                failures.append(dp)
                yield f"[The query on {dp} failed: {payload}]\n"
        if facts:
            break
    if facts is None:      # no number: say so plainly instead of letting a 0.5B model improvise one
        yield "\n" + no_answer(first, failures)
        return
    yield "\n"
    for chunk in _llm().chat.completions.create(model=MODEL, messages=explain(question, facts[:MAX_RESULT_CHARS + 800]),
                                                stream=True, max_tokens=300, temperature=0.2):
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def no_answer(first: tuple[str, str, dict] | None, failures: list[str]) -> str:
    if first is None:
        return "I couldn't read any data product, so I have no numbers to share."
    title, dp, catalog = first
    names = ", ".join(m["name"] for m in catalog["measures"][:12]) or "(none found)"
    dims = ", ".join(d["name"] for d in catalog["dimensions"][:8]) or "(none found)"
    if failures:
        return (f"I found a measure that fits, but the query failed on {', '.join(failures)} (see above), so I have no number. "
                f"Try another data product with dp:<name>.")
    return (f"I couldn't match that to a measure, so I have no number to give. {title} ({dp}) can answer questions about: {names}. "
            f"You can split them by: {dims}. Ask again using those words, or pick a data product with dp:<name>.")


def _llm() -> OpenAI:
    global _client
    _client = _client or OpenAI(api_key=os.environ.get("DAPP_LLM_KEY") or None)
    return _client


# ------------------------------------------------------------------ chat + history (same shape as examples/chatbot)
def who(request: Request) -> str:
    return request.headers.get("x-dapp-user", "")


def history(user: str):
    db = objects.open("chat", for_user=user or None)
    if user not in _ready:
        db.sql("create table if not exists chats (id text primary key, title text, "
               "created text default current_timestamp, updated text default current_timestamp)")
        db.sql("create table if not exists msgs (id integer primary key autoincrement, chat_id text, role text, "
               "content text, model text, at text default current_timestamp)")
        if user:
            try:
                db.share(user)          # theirs to read on the dashboard's Storage page
            except ObjectsError as e:
                log.info("not shared back to %s: %s", user, e)
        _ready.add(user)
    return db


def start_turn(user: str, chat_id: str | None, text: str) -> str | None:
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


def finish_turn(user: str, chat_id: str, reply: str) -> None:
    if not reply.strip():
        return
    try:
        db = history(user)
        db.sql("insert into msgs (chat_id, role, content, model) values (?, 'assistant', ?, ?)", [chat_id, reply, MODEL])
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


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    user = who(request)
    if not req.messages or req.messages[-1].role != "user":
        raise HTTPException(400, "the last message must be a question")
    question = req.messages[-1].content.strip()[:1000]
    chat_id = start_turn(user, req.chat_id, question) if HISTORY else None

    def stream():
        parts: list[str] = []
        try:
            try:
                for piece in answer(question, user):
                    parts.append(piece)
                    yield piece
            except Exception as e:  # show the reason in the chat instead of a dead stream
                log.exception("answer failed")
                msg = f"\n\n[error: {type(e).__name__}: {e}]"
                parts.append(msg)
                yield msg
        finally:
            if chat_id:
                finish_turn(user, chat_id, "".join(parts))

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8",
                             headers={"X-Chat-Id": chat_id} if chat_id else {})
