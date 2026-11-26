"""dapp as an MCP server (stdio). Register with Claude Code:

    claude mcp add dapp -- dapp mcp
    claude mcp add dapp -e DATAOS_RUN_AS_USER=... -e DATAOS_RUN_AS_APIKEY=... -- dapp --context ai mcp

Tools map 1:1 to Platform methods, so an agent gets the same behaviour as `dapp ... --json`.
Destructive tools need confirm=true so an agent has to state intent explicitly.
"""

from __future__ import annotations

from typing import Any

import yaml

from .client import DataOS
from .config import load_context
from .platform import Platform
from .spec import AppSpec, dump_yaml, wire_manifest


def build_server(context: str | None = None, tenant: str | None = None):
    try:                                   # mcp 2.x renamed FastMCP to MCPServer
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:                    # mcp 1.x
        from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "dapp",
        instructions=(
            "Deploy and manage applications on DataOS. Describe an app as a dapp.yaml spec "
            "(name, description, image, port, compute, env, secrets, llm, public). "
            "Call plan_app first to show the generated manifest, then deploy_app. "
            "Never put secret values in env; use set_secret. Set llm=true to meter LLM tokens. "
            "Apps are private by default (DataOS sign-in); share them with allow_access."
        ),
    )

    def platform() -> Platform:
        return Platform(DataOS(load_context(context, tenant)))

    def spec_from(spec_yaml: str) -> AppSpec:
        return AppSpec.model_validate(yaml.safe_load(spec_yaml) or {})

    @mcp.tool()
    def whoami() -> dict[str, Any]:
        """The DataOS instance, tenant and user these tools act as."""
        ctx = load_context(context, tenant)
        return {"context": ctx.name, "fqdn": ctx.fqdn, "tenant": ctx.tenant, "user": ctx.user_id}

    @mcp.tool()
    def list_apps(everyone: bool = False) -> list[dict[str, Any]]:
        """List apps. By default only the caller's; everyone=true lists the whole tenant."""
        return platform().list_apps(everyone=everyone)

    @mcp.tool()
    def plan_app(spec_yaml: str, compute: str | None = None) -> str:
        """Validate a dapp.yaml spec and return the DataOS manifest it generates (no changes made)."""
        plan = platform().plan(spec_from(spec_yaml), compute=compute)
        return dump_yaml(*[wire_manifest(m) for m in plan.manifests]) + (
            "\n# " + "\n# ".join(plan.notes) if plan.notes else "")

    @mcp.tool()
    def deploy_app(spec_yaml: str, compute: str | None = None, lint_only: bool = False) -> dict[str, Any]:
        """Create or update an app from a dapp.yaml spec. lint_only=true validates without deploying."""
        return platform().deploy(spec_from(spec_yaml), compute=compute, lint=lint_only)

    @mcp.tool()
    def app_status(name: str) -> dict[str, Any]:
        """Status, pods and runtime details of an app."""
        return platform().status(name)

    @mcp.tool()
    def app_logs(name: str, tail: int = 100, pod: str | None = None) -> str:
        """Recent container logs of an app (the running pod's last lines)."""
        return platform().logs_text(name, tail=tail, node=pod)

    @mcp.tool()
    def search_logs(name: str, query: str = "*", since: str | None = None, start: str | None = None, end: str | None = None,
                    stream: str | None = None, pod: str | None = None, limit: int = 100, offset: int = 0,
                    kind: str = "app") -> dict[str, Any]:
        """Search the stored logs of an app (or kind=nilus/service/workflow) over any time range, old and crashed pods
        included. query: a keyword or "*". Time: since="1h" (30m, 24h, 7d) or start/end (ISO like 2026-11-26T16:00:00Z,
        epoch seconds, or "2h ago"); default the last hour. stream: stdout or stderr. Returns total and lines (oldest first)."""
        return platform().search_logs(name, kind=kind, query=query, since=since, start=start, end=end, stream=stream,
                                      pod=pod, limit=limit, offset=offset)

    @mcp.tool()
    def scale_app(name: str, replicas: int) -> dict[str, Any]:
        """Set an app's replica count."""
        return platform().scale(name, replicas)

    @mcp.tool()
    def delete_app(name: str, confirm: bool, purge: bool = False) -> dict[str, Any]:
        """Delete an app. confirm must be true. purge=true also deletes its secrets, grants and LLM keys."""
        if not confirm:
            return {"error": "not deleted: pass confirm=true after the user agrees"}
        return platform().delete(name, purge=purge)

    @mcp.tool()
    def list_secrets(app: str) -> list[dict[str, Any]]:
        """Secrets an app uses (names only, never values)."""
        return platform().list_secrets(app)

    @mcp.tool()
    def set_secret(app: str, secret: str, data: dict[str, str]) -> dict[str, Any]:
        """Create/replace a key-value secret (all keys) and inject it into the app as env vars."""
        return platform().set_secret(app, secret, data)

    @mcp.tool()
    def delete_secret(app: str, secret: str, confirm: bool) -> dict[str, Any]:
        """Detach and delete a secret. confirm must be true."""
        if not confirm:
            return {"error": "not deleted: pass confirm=true after the user agrees"}
        return platform().delete_secret(app, secret)

    @mcp.tool()
    def list_access(app: str) -> dict[str, Any]:
        """Who can open an app: owners, allowed DataOS users and Heimdall roles."""
        return platform().allowlist(app)

    @mcp.tool()
    def allow_access(app: str, users: list[str] | None = None, roles: list[str] | None = None) -> dict[str, Any]:
        """Let DataOS users ("*" = anyone signed in) or roles open the app. The app restarts."""
        return platform().allow(app, users or [], roles or [])

    @mcp.tool()
    def deny_access(app: str, users: list[str] | None = None, roles: list[str] | None = None,
                    confirm: bool = False) -> dict[str, Any]:
        """Remove users or roles from the app's allow-list. confirm must be true."""
        if not confirm:
            return {"error": "not changed: pass confirm=true after the user agrees"}
        return platform().deny(app, users or [], roles or [])

    @mcp.tool()
    def grant_access(app: str, user: str, use_case: str,
                     values: dict[str, list[dict[str, str]]] | None = None) -> dict[str, Any]:
        """Advanced: grant a Heimdall policy use case, recorded against the app (not needed to share an app)."""
        return platform().grant_access(app, user, use_case=use_case, values=values)

    @mcp.tool()
    def revoke_access(grant: str, confirm: bool) -> dict[str, Any]:
        """Delete a grant by name. confirm must be true."""
        if not confirm:
            return {"error": "not revoked: pass confirm=true after the user agrees"}
        return platform().revoke_access(grant)

    @mcp.tool()
    def list_llm_keys() -> list[dict[str, Any]]:
        """Gateway keys of the caller's apps (prefix, app, created, revoked). The key itself is never shown."""
        return platform().llm_keys()

    @mcp.tool()
    def rotate_llm_key(app: str) -> dict[str, Any]:
        """Issue a new gateway key for an app, wait for DataOS to restart it, then revoke the old keys (~40s)."""
        return platform().rotate_llm_key(app)

    @mcp.tool()
    def revoke_llm_key(app: str, prefix: str, confirm: bool = False) -> dict[str, Any]:
        """Revoke one gateway key by prefix. The app fails its LLM calls if it still uses it. confirm must be true."""
        if not confirm:
            return {"error": "not revoked: pass confirm=true after the user agrees"}
        return platform().revoke_llm_key(app, prefix)

    @mcp.tool()
    def usage(app: str | None = None, since_hours: int = 720, group_by: str = "app,model") -> dict[str, Any]:
        """LLM token usage, plus compute for one app. group_by: comma list of app, model, day, end_user
        (end_user = the signed-in people who used the app)."""
        p = platform()
        out: dict[str, Any] = {}
        try:
            out["llm"] = p.llm_usage(app, since_hours=since_hours, group_by=group_by)
        except Exception as e:
            out["llm"] = {"error": str(e)}
        if app:
            try:
                out["compute"] = p.compute_usage(app)
            except Exception as e:
                out["compute"] = {"error": str(e)}
        return out

    return mcp


def run(context: str | None = None, tenant: str | None = None) -> None:
    try:
        server = build_server(context, tenant)
    except ImportError as e:
        raise SystemExit("MCP support needs the extra: pip install 'dataos-apps[mcp]'") from e
    server.run()
