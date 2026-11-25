"""High-level app operations. The CLI, the MCP server and the dashboard all call these, so behaviour is
identical whether a person, an agent or a browser does it.
"""

from __future__ import annotations

import copy
import difflib
import logging
import os
import re
import secrets as pysecrets
import time
from dataclasses import dataclass
from typing import Any

from .client import DataOS, DataOSError
from .config import Context, current_context_name
from .ctl import ApiWriter, Writer, make_writer
from .pack import Packed, pack
from .spec import (
    ALLOWED_ROLES_ENV,
    ALLOWED_USERS_ENV,
    DAPP_TAG,
    OBJECTS_TOOL,
    AppSpec,
    app_tag,
    llm_secret_name,
    render_app,
    render_grant,
    render_secret,
    service_name,
    subject_for,
    wire_manifest,
    yaml_decode,
)
from .usage import GatewayUsage, Prometheus, observability_url, service_of

log = logging.getLogger("dapp.platform")


class PlatformError(RuntimeError):
    pass


def gateway_public_url(ctx: Context) -> str:
    return os.environ.get("DAPP_GATEWAY_URL") or f"{ctx.base_url}/dapp-gateway"


def gateway_internal_url() -> str:
    return os.environ.get("DAPP_GATEWAY_INTERNAL_URL", "http://dapp-gateway:8080")


def llm_env(gateway: str) -> dict[str, str]:
    """Point the common SDKs at the gateway; they pick these up without code changes."""
    return {
        "ANTHROPIC_BASE_URL": f"{gateway}/anthropic",
        "OPENAI_BASE_URL": f"{gateway}/openai/v1",
        "DAPP_LLM_GATEWAY_URL": gateway,
    }


VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ID_RE = re.compile(r"^[A-Za-z0-9_.@*:-]+$")


def split_ids(csv: str) -> list[str]:
    """User/role ids from a comma list. Anything that isn't a plausible id is dropped, never carried forward."""
    return [i for i in (p.strip() for p in csv.split(",")) if ID_RE.match(i)]


def access_summary(a: dict[str, Any]) -> dict[str, Any]:
    """Who can open an app, in one glance: login (n people, roles, or everyone), public, or DataOS policy."""
    spec = a.get("spec") or {}
    envs = live_envs(a)
    if envs.get("DAPP_AUTH") == "oidc":
        users = split_ids(envs.get(ALLOWED_USERS_ENV, ""))
        owners = (spec.get("app") or {}).get("owners") or []
        return {"mode": "login", "everyone": "*" in users,
                "people": len({u for u in users if u != "*"} | set(owners)),
                "roles": len(split_ids(envs.get(ALLOWED_ROLES_ENV, "")))}
    if (((spec.get("service") or {}).get("http") or {}).get("noNetworkAuthorization")):
        return {"mode": "public"}
    return {"mode": "gateway"}


def live_envs(res: dict[str, Any]) -> dict[str, str]:
    """A live app's env values as the container sees them (stored values may carry YAML quoting)."""
    raw = ((res.get("spec") or {}).get("service") or {}).get("envs") or {}
    return {k: yaml_decode(v) for k, v in raw.items()}


def app_state(status: str | None, runtime: str | None, error: str | None) -> str:
    """One word for humans: running / failed / pending / deleting (runtime looks like 'app-service:running:1')."""
    r, s = (runtime or "").lower(), (status or "").lower()
    if "fail" in r or "error" in r or error:
        return "failed"
    if "running" in r:
        return "running"
    if "delet" in s:
        return "deleting"
    return "pending"


def flatten_logs(raw: Any) -> str:
    """Poros returns {nodeLogs: [{name: pod, logs: [{name: container, log: [lines]}]}]}."""
    if isinstance(raw, str):
        return raw
    lines: list[str] = []
    for node in (raw or {}).get("nodeLogs") or []:
        for c in node.get("logs") or []:
            lines.append(f"==> {node.get('name')} / {c.get('name')} <==")
            lines.extend(c.get("log") or [])
            lines.append("")
    return "\n".join(lines) if lines else str(raw)


@dataclass
class DeployPlan:
    manifests: list[dict[str, Any]]
    notes: list[str]
    code: Packed | None = None


class Platform:
    def __init__(self, client: DataOS, writer: Writer | None = None):
        self.client = client
        self.ctx = client.ctx
        if writer is None:
            writer = make_writer(client)
            # dataos-ctl acts on *its* current context; if dapp targets another one, go through the API.
            if writer.name == "dataos-ctl" and self.ctx.name not in ("env", current_context_name()):
                writer = ApiWriter(client)
        self.writer = writer

    # ---------- read ----------
    def list_apps(self, *, everyone: bool = False, with_access: bool = False) -> list[dict[str, Any]]:
        """with_access also asks DataOS for each app's spec, to say who can open it (a heavier call)."""
        owner = None if everyone else self.ctx.user_id
        apps = self.client.list_resources("app", owner=owner, details=with_access)
        return [self._summarize(a) for a in apps]

    def _summarize(self, a: dict[str, Any]) -> dict[str, Any]:
        tags = a.get("tags") or []
        status, runtime, error = a.get("status"), a.get("runtime"), a.get("error") or a.get("runtimeError")
        if isinstance(status, dict):  # GET returns the full status object, LIST a flattened string
            runtime = runtime or (status.get("runtimeState") or {}).get("status")
            error = error or status.get("error")
            status = status.get("aggregateStatus")
        return {
            "name": a.get("name"),
            "tenant": a.get("tenant") or self.ctx.tenant,
            "owner": a.get("owner"),
            "state": app_state(status, runtime, error),
            "status": status,
            "runtime": runtime,
            "error": error,
            "created": a.get("createdAt"),
            "managed_by_dapp": DAPP_TAG in tags,
            "url": self.app_url(a),
            **({"access": access_summary(a)} if "spec" in a else {}),
        }

    def app_url(self, a: dict[str, Any]) -> str | None:
        spec = a.get("spec") or {}
        path = ((spec.get("service") or {}).get("http") or {}).get("path") or f"/{a.get('name')}"
        return f"{self.ctx.base_url}{path}/"

    def get_app(self, name: str) -> dict[str, Any]:
        app = self.client.get_resource("app", name)
        if app is None:
            raise PlatformError(f"app '{name}' not found in tenant {self.ctx.tenant}")
        return app

    def status(self, name: str) -> dict[str, Any]:
        app = self.get_app(name)
        out = {**self._summarize(app), "spec": app.get("spec"), "service": service_name(name)}
        try:  # the generated service is a sub-resource; Poros serves its runtime on the app
            out["runtime_detail"] = self.client.runtime("app", name)
        except DataOSError as e:
            out["runtime_detail"] = {"error": str(e)}
        return out

    def logs(self, name: str, *, tail: int = 100, node: str | None = None) -> Any:
        return self.client.logs("app", name, tail=tail, node=node)

    def logs_text(self, name: str, *, tail: int = 100, node: str | None = None) -> str:
        return flatten_logs(self.logs(name, tail=tail, node=node))

    # ---------- deploy / lifecycle ----------
    def plan(self, spec: AppSpec, *, compute: str | None = None) -> DeployPlan:
        tenant = spec.tenant or self.ctx.tenant
        notes: list[str] = []
        if spec.llm:
            spec = spec.model_copy(update={"env": {**llm_env(gateway_internal_url()), **spec.env}})
            notes.append(f"LLM calls are routed via the dapp gateway; key secret: {spec.llm_secret}")
            if spec.tools:
                spec = spec.model_copy(update={"env": {
                    "DAPP_TOOLS": ",".join(spec.tools),
                    "DAPP_TOOLS_URL": f"{gateway_internal_url()}/mcp/{tenant}", **spec.env}})
                notes.append(f"MCP tools via the gateway: {', '.join(spec.tools)}")
            if OBJECTS_TOOL in spec.tools:
                spec = spec.model_copy(update={"env": {
                    "DAPP_OBJECTS_URL": f"{gateway_internal_url()}/mcp/{tenant}/{OBJECTS_TOOL}", **spec.env}})
                notes.append("durable storage: immortal-obj (objects are this app's own: app:" + spec.name + ")")
        code = None
        if spec.runtime:
            code = pack(spec.code)
            # the version in the app's env changes with the code, so a code change always rolls the app
            spec = spec.model_copy(update={"env": {**spec.env, "DAPP_CODE_VERSION": code.sha256[:12]}})
            notes.append(f"runtime {spec.runtime}: {len(code.files)} files, {code.size // 1024 + 1} KB packed, "
                         f"version {code.sha256[:12]}, stored in secret {spec.code_secret}")
        if spec.uses_login:
            spec = self._keep_live_access(spec)
            notes.append(f"DataOS sign-in inside the app (dapp_login); allowed: owners + {spec.access.users or '-'}"
                         f" roles {spec.access.roles or '-'}")
        elif spec.public:
            notes.append("public: anyone with the URL can open this app")
        app = render_app(spec, tenant=tenant, user_id=self.ctx.user_id, fqdn=self.ctx.fqdn,
                         compute=compute or (None if spec.compute else os.environ.get("DAPP_DEFAULT_COMPUTE")))
        return DeployPlan([app], notes, code)

    def _keep_live_access(self, spec: AppSpec) -> AppSpec:
        """`dapp access allow` edits the live app; a later deploy from dapp.yaml must not drop those people."""
        try:
            live = self.client.get_resource("app", spec.name)
        except DataOSError:
            live = None
        envs = live_envs(live or {})
        users = split_ids(envs.get(ALLOWED_USERS_ENV, ""))
        roles = split_ids(envs.get(ALLOWED_ROLES_ENV, ""))
        if not users and not roles:
            return spec
        access = spec.access.model_copy(update={
            "users": sorted(set(spec.access.users) | set(users)),
            "roles": sorted(set(spec.access.roles) | set(roles)),
        })
        return spec.model_copy(update={"access": access})

    def deploy(self, spec: AppSpec, *, compute: str | None = None, lint: bool = False,
               rotate_llm_key: bool = False) -> dict[str, Any]:
        plan = self.plan(spec, compute=compute)
        tenant = plan.manifests[0]["tenant"]
        manifests = list(plan.manifests)
        if spec.llm and not lint:
            existing = self.client.get_resource("secret", spec.llm_secret)
            if existing is None or rotate_llm_key:
                key = self._gateway().issue_key(spec.name, tenant, rotate=rotate_llm_key)["key"]
                manifests.insert(0, render_secret(
                    spec.llm_secret, tenant=tenant, app=spec.name,
                    description=f"dapp LLM gateway key for {spec.name}",
                    data={"DAPP_LLM_KEY": key},  # the one key the app holds; the real provider keys stay in the gateway
                ))
                plan.notes.append("issued a new LLM gateway key" if existing is None else "rotated LLM gateway key")
        if plan.code and not lint:
            manifests.insert(0, render_secret(spec.code_secret, tenant=tenant, app=spec.name, data=plan.code.data,
                                              description=f"dapp source code of {spec.name} "
                                                          f"({plan.code.sha256[:12]})"))
        if spec.uses_login and not lint and self.client.get_resource("secret", spec.oidc_secret) is None:
            manifests.insert(0, render_secret(
                spec.oidc_secret, tenant=tenant, app=spec.name,
                description=f"dapp sign-in secrets for {spec.name}",
                data={"clientSecret": pysecrets.token_urlsafe(32), "DAPP_SESSION_KEY": pysecrets.token_urlsafe(32)},
            ))
            plan.notes.append(f"created OIDC client secret {spec.oidc_secret}")
        output = self._write(*manifests, lint=lint)
        if spec.tools and not lint:
            plan.notes.extend(self._allow_app_on_tools(spec.name, spec.tools))
        app = manifests[-1]
        return {
            "app": spec.name,
            "tenant": tenant,
            "url": f"{self.ctx.base_url}{app['spec']['service']['http']['path']}/",
            "linted_only": lint,
            "writer": self.writer.name,
            "notes": plan.notes,
            "output": output,
        }

    def scale(self, name: str, replicas: int) -> dict[str, Any]:
        if replicas < 1:
            raise PlatformError("replicas must be >= 1 (use `dapp rm` to remove an app)")
        app = self._editable(self.get_app(name))
        app["spec"]["replicas"] = replicas
        return {"app": name, "replicas": replicas, "output": self._write(app)}

    def delete(self, name: str, *, purge: bool = False) -> dict[str, Any]:
        out = [self.writer.delete("app", name)]
        if purge:
            for s in self.list_secrets(name):
                out.append(self.writer.delete("secret", s["name"]))
            for g in self.list_access(name):
                out.append(self.writer.delete("grant", g["name"]))
            try:
                self._gateway().revoke_keys(name, self.ctx.tenant)
                out.append("revoked LLM gateway keys")
            except Exception as e:  # gateway may not be installed
                out.append(f"LLM keys not revoked: {e}")
        return {"app": name, "purged": purge, "output": "\n".join(out)}

    # ---------- secrets ----------
    def list_secrets(self, app: str) -> list[dict[str, Any]]:
        """Secrets dapp created for the app plus any the app references. Values are never returned."""
        tagged = {s["name"]: s for s in self.client.list_resources("secret", tags=[app_tag(app)])}
        try:
            refs = (self.get_app(app).get("spec") or {}).get("service", {}).get("secrets") or []
        except PlatformError:
            refs = []
        out = [{"name": n, "owner": s.get("owner"), "managed_by_dapp": True, "referenced": False}
               for n, s in tagged.items()]
        for r in refs:
            sid = r.get("id", "")
            short = sid.split(":", 1)[-1]
            row = next((o for o in out if o["name"] == short), None)
            if row:
                row["referenced"] = True
            else:
                out.append({"name": sid, "owner": None, "managed_by_dapp": False, "referenced": True})
        return out

    def set_secret(self, app: str, secret: str, data: dict[str, str], *, attach: bool = True) -> dict[str, Any]:
        """Create/replace a key-value secret (all keys; DataOS can't merge unseen values) and attach it."""
        if not data:
            raise PlatformError("no KEY=VALUE pairs given")
        if not secret.startswith("dapp-"):
            secret = f"dapp-{app}-{secret}"  # so the owner's single dapp-** read grant covers it
        tenant = self.ctx.tenant
        manifests = [render_secret(secret, tenant=tenant, data=data, app=app,
                                   description=f"dapp secret for {app}")]
        if attach:
            a = self._editable(self.get_app(app))
            svc = a["spec"].setdefault("service", {})
            ids = [s.get("id") for s in svc.get("secrets") or []]
            if secret not in ids and f"{tenant}:{secret}" not in ids:
                svc.setdefault("secrets", []).append({"id": secret})
                manifests.append(a)
        out = self._write(*manifests)
        return {"app": app, "secret": secret, "keys": sorted(data), "attached": attach, "output": out}

    def delete_secret(self, app: str, secret: str) -> dict[str, Any]:
        a = self._editable(self.get_app(app))
        svc = a["spec"].get("service", {})
        before = svc.get("secrets") or []
        after = [s for s in before if s.get("id") not in (secret, f"{self.ctx.tenant}:{secret}")]
        out = []
        if len(after) != len(before):
            svc["secrets"] = after
            out.append(self._write(a))
        out.append(self.writer.delete("secret", secret))
        return {"app": app, "secret": secret, "output": "\n".join(out)}

    def apply_secret_file(self, path: str, env: dict[str, str], *, dry_run: bool = False) -> dict[str, Any]:
        """Apply secret manifests whose values are ${VAR} references, never literals in the file.

        Refuses unset or empty variables (dataos-ctl would silently write "" and wipe a working key) and files
        that still hold <placeholders> (the *.generated.yaml shapes). Returns names and key names, never values.
        """
        import yaml

        with open(path) as f:
            docs = [d for d in yaml.safe_load_all(f) if d]  # comments are gone after parsing
        problems: list[str] = []

        def fill(value: Any, where: str) -> Any:
            if isinstance(value, dict):
                return {k: fill(v, f"{where}.{k}") for k, v in value.items()}
            if isinstance(value, list):
                return [fill(v, where) for v in value]
            if not isinstance(value, str):
                return value
            if value.startswith("<") and value.endswith(">"):
                problems.append(f"{where} is a placeholder ({value[:40]}): this file is a shape, dapp creates it")
            for var in VAR_RE.findall(value):
                if not env.get(var):
                    problems.append(f"{where} needs ${{{var}}}, which is unset or empty")
            return VAR_RE.sub(lambda m: env.get(m.group(1), ""), value)

        filled = [fill(d, d.get("name", "?")) for d in docs]
        bad = [d.get("name") for d in filled if d.get("type") != "secret"]
        if bad:
            problems.append(f"only type: secret is accepted here, got {bad}")
        if problems:
            raise PlatformError("not applied:\n  " + "\n  ".join(problems))
        out = "dry run: nothing applied" if dry_run else self._write(*filled)
        return {"secrets": [{"name": d["name"], "tenant": d.get("tenant"),
                             "keys": sorted((d.get("spec") or {}).get("data") or {})} for d in filled],
                "applied": not dry_run, "output": out}

    # ---------- access (app-level allow-list, enforced by dapp_login) ----------
    def allowlist(self, app: str) -> dict[str, Any]:
        a = self.get_app(app)
        spec = a.get("spec") or {}
        envs = live_envs(a)
        login = envs.get("DAPP_AUTH") == "oidc"
        public = bool(((spec.get("service") or {}).get("http") or {}).get("noNetworkAuthorization")) and not login
        return {
            "app": app,
            "mode": "login" if login else ("public" if public else "gateway"),
            "owners": (spec.get("app") or {}).get("owners") or [],
            "users": split_ids(envs.get(ALLOWED_USERS_ENV, "")),
            "roles": split_ids(envs.get(ALLOWED_ROLES_ENV, "")),
        }

    def resolve_users(self, typed: list[str]) -> tuple[list[str], dict[str, str]]:
        """Turn what people typed into real DataOS user ids: an id in any letter case, or an email. Anything that
        matches nobody raises with the closest ids, so a typo can't silently "grant" access to a person who
        doesn't exist. If DataOS won't let us read the user list we can't check, and accept what was typed."""
        names = [t for t in typed if t != "*"]
        if not names:
            return list(typed), {}
        try:
            people = self.client.list_users()
        except DataOSError:
            return list(typed), {}
        by_id = {u["id"].lower(): u["id"] for u in people}
        by_email = {u["email"].lower(): u["id"] for u in people if u.get("email")}
        ids: list[str] = []
        changed: dict[str, str] = {}
        problems: list[str] = []
        for t in typed:
            if t == "*":
                ids.append(t)
                continue
            key = t.lower()
            found = by_id.get(key) or by_email.get(key)
            if found:
                ids.append(found)
                if found != t:
                    changed[t] = found
                continue
            near = difflib.get_close_matches(key, list(by_id), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(by_id[n] for n in near)}?" if near else ""
            problems.append(f"'{t}' is not a DataOS user.{hint}")
        if problems:
            raise PlatformError("not added: " + " ".join(problems))
        return ids, changed

    def allow(self, app: str, users: list[str] = (), roles: list[str] = (), *, verify: bool = True) -> dict[str, Any]:
        users, resolved = self.resolve_users(list(users)) if verify and users else (list(users), {})
        return {**self._edit_allowlist(app, add_users=users, add_roles=roles), "resolved": resolved}

    def deny(self, app: str, users: list[str] = (), roles: list[str] = ()) -> dict[str, Any]:
        return self._edit_allowlist(app, del_users=users, del_roles=roles)

    def _edit_allowlist(self, app: str, add_users=(), add_roles=(), del_users=(), del_roles=()) -> dict[str, Any]:
        a = self._editable(self.get_app(app))
        svc = a["spec"].setdefault("service", {})
        envs = svc.setdefault("envs", {})
        if envs.get("DAPP_AUTH") != "oidc":
            raise PlatformError(f"{app} doesn't use dapp sign-in (login: true); its access can't be listed here")
        owners = set((a["spec"].get("app") or {}).get("owners") or [])
        if owners & set(del_users):
            raise PlatformError(f"owners always have access: {', '.join(sorted(owners & set(del_users)))}")
        users = (set(split_ids(envs.get(ALLOWED_USERS_ENV, ""))) | set(add_users)) - set(del_users)
        roles = (set(split_ids(envs.get(ALLOWED_ROLES_ENV, ""))) | set(add_roles)) - set(del_roles)
        envs[ALLOWED_USERS_ENV] = ",".join(sorted(users | owners))
        envs[ALLOWED_ROLES_ENV] = ",".join(sorted(roles))
        out = self._write(a)
        return {**self.allowlist_from(a), "output": out, "note": "the app restarts to pick up the change"}

    @staticmethod
    def allowlist_from(a: dict[str, Any]) -> dict[str, Any]:
        envs = live_envs(a)
        return {"app": a.get("name"), "users": split_ids(envs.get(ALLOWED_USERS_ENV, "")),
                "roles": split_ids(envs.get(ALLOWED_ROLES_ENV, ""))}

    # ---------- Heimdall grants (advanced) ----------
    def list_access(self, app: str) -> list[dict[str, Any]]:
        grants = self.client.list_resources("grant", tags=[app_tag(app)], details=True)
        rows = []
        for g in grants:
            spec = g.get("spec") or g.get("grant") or {}
            rows.append({
                "name": g.get("name"),
                "use_case": spec.get("policy_use_case_id"),
                "subjects": spec.get("subjects"),
                "values": spec.get("values"),
                "status": g.get("status"),
            })
        return rows

    def grant_access(self, app: str, user: str, *, use_case: str,
                     values: dict[str, list[dict[str, str]]] | None = None) -> dict[str, Any]:
        self.get_app(app)
        safe_user = "".join(c if c.isalnum() else "-" for c in user.split(":")[-1].lower())[:30]
        name = f"dapp-{app}-{safe_user}-{pysecrets.token_hex(2)}"
        grant = render_grant(
            name=name, tenant=self.ctx.tenant, use_case=use_case, subjects=[subject_for(user)],
            values=values or {}, requester=self.ctx.user_id or "unknown",
            notes=f"dapp: {user} gets {use_case} on app {app}", app=app,
        )
        return {"grant": name, "output": self.writer.apply(grant)}

    def revoke_access(self, grant_name: str) -> dict[str, Any]:
        return {"grant": grant_name, "output": self.writer.delete("grant", grant_name)}

    # ---------- LLM gateway keys ----------
    def llm_keys(self) -> list[dict[str, Any]]:
        """The caller's gateway keys: prefix, app, state. The key itself can't be shown, the gateway keeps a hash."""
        return self._gateway().list_keys()

    def personal_llm_key(self, *, rotate: bool = False) -> dict[str, Any]:
        """Your own gateway key (app user:<you>), for calling the LLMs from outside DataOS."""
        r = self._gateway().issue_personal_key(self.ctx.tenant, rotate=rotate)
        return {**r, "base_url": f"{gateway_public_url(self.ctx)}/openai/v1"}

    def _llm_secret_of(self, app: str) -> str:
        a = self.get_app(app)
        want = llm_secret_name(app)
        ids = [s.get("id", "") for s in ((a.get("spec") or {}).get("service") or {}).get("secrets") or []]
        if not any(i.split(":")[-1] == want for i in ids):
            raise PlatformError(f"{app} doesn't use the LLM gateway: set `llm: true` in its dapp.yaml and deploy")
        return want

    def _app_pods(self, app: str) -> set[str]:
        nodes = (self.client.runtime("app", app).get("nodeDetails") or [])
        return {n["name"] for n in nodes if n.get("type") == "cgroup-stateless" and n.get("name")}

    def start_llm_rotation(self, app: str) -> dict[str, Any]:
        """Step 1: issue a new key and store it in the app's secret. The old keys stay valid for now, because the
        running pod still has one until DataOS restarts the app (it does, by itself, when the secret changes)."""
        secret = self._llm_secret_of(app)
        tenant = self.ctx.tenant
        old = [k["prefix"] for k in self.llm_keys() if k["app"] == app and k["tenant"] == tenant and not k["revoked"]]
        before = sorted(self._app_pods(app))
        issued = self._gateway().issue_key(app, tenant, rotate=False)
        out = self._write(render_secret(secret, tenant=tenant, app=app, data={"DAPP_LLM_KEY": issued["key"]},
                                        description=f"dapp LLM gateway key for {app}"))
        return {"app": app, "new_key_prefix": issued["prefix"], "secret": secret, "old_prefixes": old,
                "before_pods": before, "output": out}

    def finish_llm_rotation(self, app: str, old_prefixes: list[str], before_pods: list[str], *,
                            timeout: float = 150, poll: float = 5) -> dict[str, Any]:
        """Step 2: once every old pod is gone (so nothing still uses an old key), revoke the old keys."""
        deadline = time.monotonic() + timeout
        rolled = False
        while time.monotonic() < deadline:
            now = self._app_pods(app)
            if now and not (now & set(before_pods)):
                rolled = True
                break
            time.sleep(poll)
        revoked: list[str] = []
        if rolled:
            for prefix in old_prefixes:
                try:
                    self.revoke_llm_key(app, prefix)
                    revoked.append(prefix)
                except Exception as e:  # already revoked is fine
                    log.warning("could not revoke %s for %s: %s", prefix, app, e)
        return {"app": app, "rolled_over": rolled, "revoked": revoked,
                "still_active": [] if rolled else old_prefixes}

    def rotate_llm_key(self, app: str, *, timeout: float = 150) -> dict[str, Any]:
        """New key in, app restarted by DataOS, then old keys revoked: no failed calls in between."""
        started = self.start_llm_rotation(app)
        done = self.finish_llm_rotation(app, started["old_prefixes"], started["before_pods"], timeout=timeout)
        return {**{k: started[k] for k in ("app", "new_key_prefix", "secret")}, **done}

    def revoke_llm_key(self, app: str, prefix: str) -> dict[str, Any]:
        """Kill one key. If it is the app's current one the app gets 401s until the key is rotated."""
        return {"app": app, **self._gateway().revoke_key(app, self.ctx.tenant, prefix)}

    def list_users(self) -> list[dict[str, str]]:
        return self.client.list_users()

    # ---------- API keys (DataOS) ----------
    def list_keys(self) -> list[dict[str, Any]]:
        return self.client.list_apikeys()

    def create_key(self, name: str, duration: str = "720h", description: str = "") -> dict[str, Any]:
        return self.client.create_apikey(name, duration=duration, description=description)

    def delete_key(self, name: str, *, force: bool = False) -> None:
        mine = {k["name"]: k for k in self.client.list_apikeys()}
        if name not in mine:
            raise PlatformError(f"no API key named {name}")
        if mine[name].get("system") and not force:
            raise PlatformError(f"{name} was created by DataOS to run your apps as you; deleting it breaks them "
                                "(pass force only if you know it is unused)")
        self.client.delete_apikey(name)

    # ---------- usage ----------
    def compute_usage(self, app: str) -> dict[str, Any]:
        url = os.environ.get("DAPP_PROMETHEUS_URL") or observability_url(self.ctx.base_url)
        if app.startswith("user:"):  # a personal LLM key: calls from a laptop, no pods in DataOS
            return {"note": "personal LLM key: no compute in DataOS"}
        service = self.service_name(app)
        if not service:
            return {"error": f"{app} has no running pods, so its compute can't be told apart from same-named apps"}
        return Prometheus(url, headers=self.ctx.auth_headers()).app_now(app, self.ctx.tenant, service)

    def service_name(self, app: str) -> str | None:
        """The DataOS service behind your app; metrics are filtered by it (job_name alone is shared by same-named
        apps of other people)."""
        try:
            return service_of(self.client.runtime("app", app))
        except DataOSError:
            return None

    def llm_usage(self, app: str | None = None, since_hours: int = 24 * 30,
                  group_by: str = "app,model") -> dict[str, Any]:
        """Tokens and cost. group_by can include end_user: the signed-in people who used the app."""
        return self._gateway().summary(app=app, since_hours=since_hours, group_by=group_by)

    # ---------- MCP tools (through the gateway) ----------
    def tools(self) -> list[dict[str, Any]]:
        """Tools you own or may use, in this tenant."""
        return self._gateway().list_tools(self.ctx.tenant)

    def tool(self, name: str) -> dict[str, Any]:
        return self._gateway().get_tool(self.ctx.tenant, name)

    def tool_url(self, name: str) -> str:
        return f"{gateway_public_url(self.ctx)}/mcp/{self.ctx.tenant}/{name}"

    def save_tool(self, name: str, *, url: str, description: str = "", auth_header: str = "",
                  auth_value: str | None = None, allow: list[str] | None = None) -> dict[str, Any]:
        """Register an MCP server (or update yours). auth_value goes to the gateway only, encrypted there."""
        if allow is not None:
            allow = self._tool_ids(allow)
        if auth_value and not auth_header and not auth_value.startswith(("Bearer ", "Basic ")):
            auth_value = f"Bearer {auth_value}"   # a bare token goes in Authorization: Bearer <token>
        if auth_value is not None and not auth_header:
            auth_header = "Authorization" if auth_value else ""
        body = {"tenant": self.ctx.tenant, "name": name, "url": url, "description": description,
                "auth_header": auth_header, "auth_value": auth_value, "allow": allow}
        return {**self._gateway().save_tool(body), "mcp_url": self.tool_url(name)}

    def delete_tool(self, name: str) -> dict[str, Any]:
        return self._gateway().delete_tool(self.ctx.tenant, name)

    def tool_allow(self, name: str, who: list[str]) -> dict[str, Any]:
        """who: DataOS user ids or emails (checked against Heimdall), "*" for every DataOS user, app:<name>."""
        return self._gateway().tool_access(self.ctx.tenant, name, self._tool_ids(who), [])

    def tool_deny(self, name: str, who: list[str]) -> dict[str, Any]:
        return self._gateway().tool_access(self.ctx.tenant, name, [], list(who))

    def tool_usage(self, name: str | None = None, *, since_hours: int = 24 * 30,
                   group_by: str = "tool,caller") -> dict[str, Any]:
        return self._gateway().tool_usage(tool=name, tenant=self.ctx.tenant, since_hours=since_hours,
                                          group_by=group_by)

    def _allow_app_on_tools(self, app: str, tools: list[str]) -> list[str]:
        """Let the app call each tool. Works for tools you own; for someone else's, say who has to do it."""
        notes = []
        for t in tools:
            if t == OBJECTS_TOOL:
                continue                            # open to every app (app:*); each app only sees its own objects
            try:
                self._gateway().tool_access(self.ctx.tenant, t, [f"app:{app}"], [])
                notes.append(f"tool {t}: app:{app} allowed")
            except Exception as e:
                notes.append(f"tool {t}: could not allow app:{app} ({e}); its owner must run "
                             f"`dapp tool allow {t} app:{app}`")
        return notes

    def _tool_ids(self, who: list[str]) -> list[str]:
        people = [w for w in who if w != "*" and not w.startswith("app:")]
        ids, _ = self.resolve_users(people) if people else ([], {})
        return [w for w in who if w == "*" or w.startswith("app:")] + ids

    # ---------- helpers ----------
    def _gateway(self) -> GatewayUsage:
        return GatewayUsage(gateway_public_url(self.ctx), self.ctx.auth_headers())

    def _write(self, *manifests: dict[str, Any], lint: bool = False) -> str:
        """The one place manifests leave dapp: env values get quoted for the DataOS template here."""
        return self.writer.apply(*[wire_manifest(m) for m in manifests], lint=lint)

    @staticmethod
    def _editable(res: dict[str, Any]) -> dict[str, Any]:
        """Strip server-side fields so the resource can be re-applied."""
        keep = ("name", "version", "type", "tenant", "tags", "description", "title", "owner", "spec")
        out = copy.deepcopy({k: res[k] for k in keep if k in res})
        out.setdefault("spec", {})
        service = out["spec"].get("service") or {}
        if service.get("envs"):  # edit plain values; _write quotes them again on the way out
            service["envs"] = {k: yaml_decode(v) for k, v in service["envs"].items()}
        return out
