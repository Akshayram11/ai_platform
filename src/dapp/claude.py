"""A personal Claude Code on DataOS, deployed from the dashboard's Claude Code tab (or `dapp init --template claude-code`).

One instance per person: its pod runs as that person (DataOS gives it their identity), so everything it deploys is theirs and
DataOS permissions apply, and nobody else can read its files or its Claude sign-in. Their code and conversations are saved in
their own immortal-obj objects (`workspace`, `claude-sessions`), so stopping the instance (deleting the app) keeps them: starting it
again restores them. An optional Claude token (`claude setup-token`) kept as a DataOS secret keeps them signed in to Claude.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from .models import mebibytes, milli_cpu
from .spec import AppSpec

CLAUDE_CODE_IMAGE = "docker.io/tmdcio/dapp-claude-code:0.1.7"   # built from apps/claude-code
CLAUDE_TAG = "dapp-claude"
TOKEN_KEY = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULTS = {"cpu": "250m", "cpu_limit": "2", "memory": "1Gi", "memory_limit": "2Gi"}   # real use peaks near 350 MiB; builds need the burst


def instance_name(user_id: str) -> str:
    """claude-<person>: lower-case letters, digits and '-', at most 48 characters (an app name)."""
    slug = re.sub(r"[^a-z0-9]+", "-", user_id.lower()).strip("-") or "me"
    return f"claude-{slug}"[:48].rstrip("-")


def token_secret(app: str) -> str:
    return f"dapp-{app}-anthropic"


def clean_token(raw: str) -> str:
    """A pasted `claude setup-token` output: one line without spaces (a wrapped token comes in two lines)."""
    token = re.sub(r"\s+", "", raw or "")
    if token and (len(token) < 40 or not re.fullmatch(r"[A-Za-z0-9._\-]+", token)):
        raise ValueError("that doesn't look like a Claude token: run `claude setup-token` and paste what it prints")
    return token


class ClaudeDeploy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu: str = DEFAULTS["cpu"]
    cpu_limit: str = DEFAULTS["cpu_limit"]
    memory: str = DEFAULTS["memory"]
    memory_limit: str = DEFAULTS["memory_limit"]
    compute: str = ""

    @field_validator("compute")
    @classmethod
    def _compute(cls, v: str) -> str:
        if v and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError("compute: a compute resource name such as engineering-compute")
        return v

    def check(self) -> ClaudeDeploy:
        cpu, cpu_limit = milli_cpu(self.cpu), milli_cpu(self.cpu_limit)
        mem, mem_limit = mebibytes(self.memory), mebibytes(self.memory_limit)
        if not 100 <= cpu <= cpu_limit <= 8000:
            raise ValueError("cpu: the request must be at least 100m and not above the limit, and the limit at most 8 cores")
        if not 512 <= mem <= mem_limit <= 16384:
            raise ValueError("memory: the request must be at least 512Mi and not above the limit, and the limit at most 16Gi "
                             "(Claude Code and its tools need about 1 Gi)")
        return self


def claude_app_spec(app: str, user_id: str, d: ClaudeDeploy, *, compute: str, fqdn: str, with_token: bool) -> AppSpec:
    """The terminal as a private app. start.sh lets only the owner through, whatever the access list says."""
    return AppSpec.model_validate({
        "name": app, "title": f"Claude Code ({user_id})", "tags": [CLAUDE_TAG],
        "description": f"Claude Code in a browser terminal for {user_id}; builds and deploys DataOS apps with dapp",
        "image": CLAUDE_CODE_IMAGE, "command": ["/opt/claude/start.sh"], "port": 8080, "compute": compute, "path": f"/{app}",
        "metrics": True, "secrets": [token_secret(app)] if with_token else [],
        "resources": {"cpu": d.cpu, "memory": d.memory, "cpuLimit": d.cpu_limit, "memoryLimit": d.memory_limit},
        "env": {"ROOT_PATH": f"/{app}", "DATAOS_FQDN": fqdn, "DAPP_DEFAULT_COMPUTE": compute},
    })


def is_claude_instance(app: dict[str, Any]) -> bool:
    return str(app.get("name", "")).startswith("claude-")
