"""`dapp doctor`: is this machine ready to deploy? One line per check, and a non-zero exit if a required one fails.

Meant for people and for agents: run it before deploying and stop on a failure instead of guessing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

import httpx

from . import __version__
from .auth import Heimdall, IdentityUnavailable
from .client import DataOS, DataOSError
from .config import ConfigError, load_context, token_seconds_left
from .ctl import find_ctl
from .platform import gateway_public_url


def _ctl_works(ctl: str | None) -> tuple[bool, str]:
    """dataos-ctl is what applies manifests (lint, stack resolution). A wrapper that needs a container, or a
    broken install, would fail every deploy, so run it once."""
    if not ctl:
        return False, "not found: dapp will use the Poros API directly"
    try:
        p = subprocess.run([ctl, "version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{ctl} could not run ({e.__class__.__name__})"
    return (True, ctl) if p.returncode == 0 else (False, f"{ctl} exited {p.returncode}: {(p.stdout + p.stderr).strip()[:100]}")


def run_checks(context: str | None = None, tenant: str | None = None, *,
               transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True, hint: str = "") -> None:
        out.append({"check": name, "ok": ok, "required": required, "detail": detail, **({"hint": hint} if hint else {})})

    add("dapp", True, f"version {__version__}, python {sys.version.split()[0]}")
    try:
        ctx = load_context(context, tenant)
    except ConfigError as e:
        add("context", False, str(e), hint="log in with `dataos-ctl login` and pick your tenant (`dataos-ctl tenant select`)")
        return out
    add("context", True, f"{ctx.fqdn} · tenant {ctx.tenant} · context {ctx.name}")
    try:
        headers = ctx.auth_headers()
    except ConfigError as e:
        add("credentials", False, str(e), hint="run `dataos-ctl login`, then `dapp doctor` again")
        return out

    token = headers.get("apikey") or headers.get("Authorization", "").removeprefix("Bearer ")
    try:
        ident = Heimdall(ctx.base_url, transport=transport).identify(token)
    except IdentityUnavailable as e:
        add("credentials", False, f"could not reach DataOS to check them: {e}", hint="check the network / VPN")
        return out
    if ident is None:
        add("credentials", False, "DataOS did not accept this login",
            hint="run `dataos-ctl login` (or create a new API key in the dashboard, Keys page, and use `dapp setup`)")
        return out
    left = token_seconds_left(token)
    when = f", session renews in {round(left / 3600, 1)} h" if left is not None and left > 0 else ""
    add("credentials", True, f"signed in as {ident.user_id}{when}")

    try:
        with DataOS(ctx, transport=transport) as client:
            apps = client.list_resources("app", owner=ctx.user_id)
            add("deploy access", True, f"can list apps in {ctx.tenant} ({len(apps)} yours)")
            try:
                computes = [c["name"] for c in client.list_resources("compute")]
                add("compute", bool(computes), ", ".join(computes) or "none visible to you", required=False,
                    hint="ask a tenant admin for a use-compute grant (deploy/grants/app-developer.template.yaml)")
            except DataOSError as e:
                add("compute", False, str(e), required=False)
    except DataOSError as e:
        add("deploy access", False, str(e), hint="ask a tenant admin for the app-developer grants")

    try:
        with httpx.Client(timeout=15, transport=transport) as hc:
            r = hc.get(f"{gateway_public_url(ctx)}/healthz")
        providers = (r.json() or {}).get("providers", {}) if r.status_code == 200 else {}
        ready = [p for p, v in providers.items() if v]
        add("LLM gateway", r.status_code == 200 and bool(ready),
            f"reachable, providers: {', '.join(ready) or 'none configured'}" if r.status_code == 200
            else f"HTTP {r.status_code}", required=False, hint="apps with llm: true need the gateway")
    except httpx.HTTPError as e:
        add("LLM gateway", False, f"unreachable ({e.__class__.__name__})", required=False)

    ctl = find_ctl()
    ctl_ok, ctl_detail = _ctl_works(ctl)
    add("dataos-ctl", ctl_ok, ctl_detail, required=False,
        hint="dapp falls back to the Poros API (DAPP_WRITER=api) when dataos-ctl isn't usable")
    add("docker", bool(shutil.which("docker")), "found" if shutil.which("docker") else
        "not installed: fine for deploying prebuilt images, needed only to build your own", required=False)
    return out
