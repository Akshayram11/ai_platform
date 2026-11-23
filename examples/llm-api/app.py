"""A non-chat LLM app: POST /summarize {"text": "..."} -> {"summary": "..."}.

No provider URL and no provider key in the code. With `llm: true` dapp sets OPENAI_BASE_URL and puts one key,
DAPP_LLM_KEY, in the app's environment; the SDK talks to the dapp gateway, which holds the real key and meters
tokens for this app.
"""

import os

from fastapi import FastAPI, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "")

app = FastAPI(title="doc-summarizer", root_path=os.environ.get("ROOT_PATH", ""))
client = OpenAI(api_key=os.environ.get("DAPP_LLM_KEY") or None)  # base URL comes from OPENAI_BASE_URL


class Doc(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "model": MODEL}


@app.post("/summarize")
def summarize(doc: Doc, request: Request) -> dict:
    extra = {"extra_body": {"reasoning_effort": EFFORT}} if EFFORT else {}
    user = request.headers.get("x-dapp-user", "")        # set by dapp_login: the signed-in DataOS user
    if user and os.environ.get("DAPP_LLM_KEY"):          # tell the gateway who, so usage shows per person
        extra["extra_headers"] = {"x-dapp-user": user}
    try:
        r = client.chat.completions.create(
            model=MODEL, **extra,
            messages=[{"role": "system", "content": "Summarize the document in at most five bullet points."},
                      {"role": "user", "content": doc.text}])
    except Exception as e:
        raise HTTPException(502, f"LLM call failed: {type(e).__name__}") from e
    return {"summary": r.choices[0].message.content, "model": r.model,
            "tokens": {"input": r.usage.prompt_tokens, "output": r.usage.completion_tokens}}


if os.environ.get("DAPP_AUTH") == "oidc":  # private by default: DataOS sign-in, or an apikey/Bearer header
    from dapp_login import protect

    app = protect(app)
