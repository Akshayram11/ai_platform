"""Entry point copied into each published source app."""

import json
import os
from pathlib import Path

from agent_runtime.engine import AgentConfig, AgentError, Chat, run_agent
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
record = json.loads((ROOT / "agent.json").read_text())
config = AgentConfig.model_validate(record["config"])
gateway = os.environ.get("OPENAI_BASE_URL", "").removesuffix("/openai/v1")
templates = Environment(loader=FileSystemLoader(ROOT), autoescape=select_autoescape())
app = FastAPI(root_path=os.environ.get("ROOT_PATH", ""))


def viewer(request: Request) -> str:
    user = request.headers.get("x-dapp-user")
    if not user:
        raise HTTPException(401, "DataOS sign-in is required.")
    return user


@app.get("/", response_class=HTMLResponse)
def page(request: Request) -> str:
    viewer(request)
    return templates.get_template("published.html").render(title=config.title, model=config.model,
                                                            version=record["version"], traced=config.trace_enabled)


@app.get("/chat.js")
def script() -> FileResponse:
    return FileResponse(ROOT / "chat.js", media_type="text/javascript")


@app.get("/agent.css")
def stylesheet() -> FileResponse:
    return FileResponse(ROOT / "agent.css", media_type="text/css")


@app.get("/healthz")
def health() -> dict:
    return {"ok": True, "version": record["version"]}


@app.post("/api/chat")
def chat(req: Chat, request: Request):
    user = viewer(request)
    try:
        return run_agent(config, req, gateway=gateway, key=os.environ.get("DAPP_LLM_KEY", ""),
                         tenant=os.environ.get("DATAOS_TENANT_ID", ""), user=user, version=record["version"])
    except AgentError as exc:
        if exc.trace_id:
            return JSONResponse({"detail": str(exc), "trace_id": exc.trace_id}, status_code=502)
        raise HTTPException(502, str(exc)) from exc
