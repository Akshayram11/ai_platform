"""A small OpenAI chatbot to show dapp end to end.

It only knows the standard OpenAI env vars, so it runs unchanged in two setups:
  - llm: true   -> dapp points OPENAI_BASE_URL at the dapp gateway and gives it one per-app key, DAPP_LLM_KEY;
                   the real OpenAI key lives only in the gateway, and tokens show in the dashboard.
  - llm: false  -> put OPENAI_API_KEY in a DataOS secret and the app calls OpenAI directly.
Local:  OPENAI_API_KEY=... uvicorn app:app --port 8080
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from openai import OpenAI
from pydantic import BaseModel

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SYSTEM = os.environ.get("SYSTEM_PROMPT", "You are a helpful, concise assistant running on DataOS.")
REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "")  # reasoning models only: none|low|medium|high
MAX_TURNS = 20

app = FastAPI(title="dapp chatbot", root_path=os.environ.get("ROOT_PATH", ""))
_client: OpenAI | None = None
PAGE = (Path(__file__).parent / "index.html").read_text()


class Msg(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Msg]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE.replace("{{MODEL}}", MODEL)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL, "via_gateway": "dapp" in os.environ.get("OPENAI_BASE_URL", "")}


def gateway_user_headers(request: Request) -> dict[str, str]:
    """Tell the dapp gateway who is chatting, so the dashboard can show usage per person. dapp_login puts the
    signed-in DataOS user in x-dapp-user. Only sent through the gateway: never to OpenAI itself."""
    user = request.headers.get("x-dapp-user", "")
    return {"x-dapp-user": user} if user and os.environ.get("DAPP_LLM_KEY") else {}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    user_headers = gateway_user_headers(request)
    history = [m.model_dump() for m in req.messages if m.role in ("user", "assistant")][-MAX_TURNS:]

    def stream():
        global _client
        try:
            # DAPP_LLM_KEY (gateway key) when deployed with llm: true; otherwise OPENAI_API_KEY from the environment
            _client = _client or OpenAI(api_key=os.environ.get("DAPP_LLM_KEY") or None)
            extra = {"extra_body": {"reasoning_effort": REASONING_EFFORT}} if REASONING_EFFORT else {}  # any SDK version
            if user_headers:
                extra["extra_headers"] = user_headers
            for chunk in _client.chat.completions.create(
                model=MODEL, messages=[{"role": "system", "content": SYSTEM}, *history], stream=True, **extra
            ):
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:  # show the reason in the chat instead of a dead stream
            yield f"\n\n[error: {type(e).__name__}: {e}]"

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


if os.environ.get("DAPP_AUTH") == "oidc":  # set by dapp when the app is private (the default)
    from dapp_login import protect

    app = protect(app)

if os.environ.get("DAPP_METRICS", "1") == "1":  # CPU/memory for the dapp dashboard, on an internal port
    import dapp_metrics

    dapp_metrics.start()
    app = dapp_metrics.instrument(app)
