"""Record one agent turn: each model call (messages in, reply out, tokens), each tool call (arguments, result) and
the question and answer. Sizes are bounded; credentials are never part of what is recorded."""

import json
import logging
import time
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

log = logging.getLogger(__name__)
TraceId = Annotated[str, StringConstraints(pattern=r"^tr-[a-f0-9]{32}$")]
TraceLabel = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9._:/-]{1,128}$")]
Timestamp = Annotated[int, Field(ge=1, le=2**63 - 1)]
TokenCount = Annotated[int, Field(ge=0, le=10**9)]
MAX_STEPS = 28
MAX_TEXT = 8_000                                  # per input or output; a turn stays well under 1 MB
TraceText = Annotated[str, Field(max_length=MAX_TEXT + 40)]


def clip(value) -> str | None:
    """Text for a trace: strings as they are, anything else as JSON, cut to MAX_TEXT."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f" …[{len(text) - MAX_TEXT} more characters]"


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["chat", "discover_tools", "execute_tool"]
    start_ns: Timestamp
    end_ns: Timestamp
    ok: bool = True
    server: TraceLabel | None = None
    function: TraceLabel | None = None
    response_model: TraceLabel | None = None
    backend: Literal["openai", "local"] | None = None
    status_code: int | None = Field(None, ge=100, le=599)
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    error_type: Literal["not_connected", "tool_error", "unavailable", "gateway_error", "invalid_response"] | None = None
    input: TraceText | None = None
    output: TraceText | None = None


class TraceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trace_id: TraceId
    model: TraceLabel
    version: int | None = Field(None, ge=1, le=2**31 - 1)
    start_ns: Timestamp
    end_ns: Timestamp
    ok: bool
    input: TraceText | None = None
    output: TraceText | None = None
    steps: list[TraceStep] = Field(max_length=MAX_STEPS)


class AgentTrace:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.trace_id = "tr-" + uuid4().hex if enabled else None
        self.start_ns = time.time_ns() if enabled else 0
        self.steps: list[dict] = []
        self.input: str | None = None
        self.output: str | None = None

    @contextmanager
    def step(self, kind, **fields):
        if not self.enabled:
            yield None
            return
        started = time.time_ns()
        step = {"kind": kind, "start_ns": started, **fields}
        try:
            yield step
        except Exception:
            step["ok"] = False
            raise
        finally:
            step["end_ns"] = max(started, time.time_ns())
            for key in ("input", "output"):
                if key in step:
                    step[key] = clip(step[key])
            if len(self.steps) < MAX_STEPS:
                self.steps.append(step)

    def export(self, gateway, key, user, model, version, ok, transport):
        if not self.enabled or not gateway or not key:
            return None
        try:
            report = TraceReport(trace_id=self.trace_id, model=model, version=version, ok=ok,
                                 start_ns=self.start_ns, end_ns=max(self.start_ns, time.time_ns()),
                                 input=clip(self.input), output=clip(self.output), steps=self.steps)
            # A fresh MLflow takes a few seconds to create the experiment for the first trace; 2s dropped it.
            with httpx.Client(timeout=8.0, transport=transport) as http:
                response = http.post(f"{gateway.rstrip('/')}/agent-traces",
                                     headers={"Authorization": f"Bearer {key}", "x-dapp-user": user},
                                     json=report.model_dump(exclude_none=True))
                response.raise_for_status()
            return self.trace_id
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Could not export agent trace (%s)", type(exc).__name__)
            return None
