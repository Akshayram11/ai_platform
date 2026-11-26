"""Connect a new source: its credentials go into a DataOS Secret, and a Depot is made on top of it.

The fields come from DataOS itself: every Depot type is registered with a JSON schema for its connection (``specSchema``) and
its credentials (``secretSchema``), served at ``GET /ds/api/v2/types``. So the form and the checks work for every type the
tenant has (PostgreSQL, MySQL, Snowflake, MongoDB, Kafka, S3...), not a hand-written list. A few presets add what a known
service needs (Supabase: PostgreSQL over TLS), and a connection string can be pasted instead of typing the fields.

Credentials are SecretStr here: they never appear in a repr, an error, a log or a page. They go only into the Secret manifest,
which the caller sends straight to DataOS.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import BaseModel, SecretStr, field_validator

SOURCE_TAG = "dapp-source"
DEPOT_NAME_RE = re.compile(r"^[a-z][a-z0-9]{2,31}$")        # like the tenant's Depots: lower-case letters and digits
NOT_SECRET = {"username", "role", "auth_mode", "storage_type", "security_protocol", "az_account_name",
              "eh_shared_access_key_name", "key_store_type"}
FILE_FIELDS = {"gcp_json_key", "ca_file", "key_store_file", "trust_store_file"}
SUBPROTOCOL = {"postgresql": "postgresql", "mysql": "mysql", "mongodb": "mongodb", "redshift": "redshift"}
SQL_PURPOSES = ("rw", "query", "scan")                         # what the tenant's working PostgreSQL Depots declare
URL_SCHEMES = {"postgres": "postgresql", "postgresql": "postgresql", "mysql": "mysql", "mongodb": "mongodb",
               "mongodb+srv": "mongodb"}


# ------------------------------------------------------------------ the types DataOS knows
@dataclass(frozen=True)
class Field_:
    key: str
    side: str                 # "spec" (connection) or "secret" (credentials)
    required: bool
    kind: str                 # text | int | params | password | file
    default: Any = None


@dataclass(frozen=True)
class SourceType:
    name: str
    fields: tuple[Field_, ...]
    spec_keys: frozenset[str] = field(default_factory=frozenset)
    secret_keys: frozenset[str] = field(default_factory=frozenset)

    def field(self, key: str) -> Field_ | None:
        return next((f for f in self.fields if f.key == key), None)


def _schema(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def parse_types(rows: list[dict[str, Any]]) -> dict[str, SourceType]:
    """`GET /ds/api/v2/types` -> the fields each Depot type asks for."""
    out: dict[str, SourceType] = {}
    for row in rows or []:
        name = row.get("name")
        if not name:
            continue
        spec, sec = _schema(row.get("specSchema")), _schema(row.get("secretSchema"))
        fields: list[Field_] = []
        req = set(spec.get("required") or [])
        for key, prop in (spec.get("properties") or {}).items():
            t = (prop or {}).get("type")
            kind = "int" if t == "integer" else ("params" if t == "object" and key == "params" else "text")
            if t == "object" and key != "params":
                continue                     # nested blocks (e.g. a lakehouse's s3:) are not offered in the simple form
            fields.append(Field_(key, "spec", key in req, kind, SUBPROTOCOL.get(name) if key == "subprotocol" else None))
        sreq = set(sec.get("required") or [])
        for key in (sec.get("properties") or {}):
            kind = "file" if key in FILE_FIELDS else ("text" if key in NOT_SECRET else "password")
            fields.append(Field_(key, "secret", key in sreq, kind))
        fields.sort(key=lambda f: (f.side != "spec", not f.required, f.key == "params", f.key))
        out[name] = SourceType(name, tuple(fields), frozenset(f.key for f in fields if f.side == "spec"),
                               frozenset(f.key for f in fields if f.side == "secret"))
    return out


# ------------------------------------------------------------------ presets for known services
@dataclass(frozen=True)
class Preset:
    key: str
    title: str
    type: str
    params: dict[str, str]
    hint: str


PRESETS: dict[str, Preset] = {p.key: p for p in (
    Preset("supabase", "Supabase", "postgresql", {"sslmode": "require"},
           "Paste the Session pooler connection string from Supabase → Connect (port 5432, host aws-…pooler.supabase.com, "
           "user postgres.<project>). The direct host db.<project>.supabase.co is IPv6-only and may not be reachable."),
    Preset("neon", "Neon", "postgresql", {"sslmode": "require"}, "Paste the connection string from the Neon console."),
    Preset("rds-postgres", "Amazon RDS PostgreSQL", "postgresql", {"sslmode": "require"}, "Host is the RDS endpoint."),
    Preset("postgresql", "PostgreSQL", "postgresql", {}, ""),
    Preset("mysql", "MySQL", "mysql", {}, ""),
    Preset("snowflake", "Snowflake", "snowflake", {}, "url is the account URL, e.g. abc123.snowflakecomputing.com."),
    Preset("mongodb", "MongoDB", "mongodb", {}, "nodes is host:port (or the Atlas host)."),
)}


# ------------------------------------------------------------------ the request
class SourceRequest(BaseModel):
    name: str
    type: str
    spec: dict[str, Any] = {}
    secret: dict[str, SecretStr] = {}
    description: str = ""
    preset: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip().lower()
        if not DEPOT_NAME_RE.match(v):
            raise ValueError("use 3-32 lower-case letters and digits, starting with a letter (Depot names have no '-')")
        return v

    @property
    def secret_name(self) -> str:
        return f"dapp-src-{self.name}"

    def __repr__(self) -> str:      # never the credentials, even by accident
        return f"SourceRequest(name={self.name!r}, type={self.type!r}, spec={self.spec!r}, secret_keys={sorted(self.secret)})"

    __str__ = __repr__


def from_url(url: str) -> tuple[str, dict[str, Any], dict[str, str]]:
    """A connection string -> (Depot type, connection fields, credentials). The password is URL-decoded and only returned."""
    u = urlsplit(url.strip())
    kind = URL_SCHEMES.get(u.scheme.lower())
    if not kind:
        raise ValueError("paste a connection string that starts with postgresql://, postgres://, mysql:// or mongodb://")
    if not u.hostname:
        raise ValueError("the connection string has no host")
    params = {k: v for k, v in parse_qsl(u.query)}
    creds = {k: v for k, v in (("username", unquote(u.username or "")), ("password", unquote(u.password or ""))) if v}
    if kind == "mongodb":
        nodes = u.netloc.rsplit("@", 1)[-1]
        spec: dict[str, Any] = {"subprotocol": u.scheme.lower(), "nodes": nodes.split(",")}
        if u.path.strip("/"):
            params.setdefault("authSource", "admin")
        return kind, {**spec, **({"params": params} if params else {})}, creds
    spec = {"subprotocol": kind, "host": u.hostname, "port": u.port or {"postgresql": 5432, "mysql": 3306}[kind]}
    db = u.path.strip("/")
    if db:
        spec["database"] = db
    if params:
        spec["params"] = params
    return kind, spec, creds


def check(req: SourceRequest, stype: SourceType | None) -> list[str]:
    """What is missing or wrong, as sentences. Never echoes a credential."""
    if stype is None:
        return [f"DataOS has no Depot type {req.type!r} in this tenant"]
    errs: list[str] = []
    for f in stype.fields:
        have = req.spec.get(f.key) if f.side == "spec" else req.secret.get(f.key)
        empty = have is None or (isinstance(have, SecretStr) and not have.get_secret_value()) or have in ("", [], {})
        if f.required and empty:
            errs.append(f"{f.key.replace('_', ' ')} is required")
        if f.side == "spec" and f.kind == "int" and not empty:
            try:
                n = int(have)
            except (TypeError, ValueError):
                errs.append(f"{f.key} must be a number")
            else:
                if f.key == "port" and not 0 < n < 65536:
                    errs.append("port must be between 1 and 65535")
    unknown = sorted(set(req.spec) - stype.spec_keys) + sorted(set(req.secret) - stype.secret_keys)
    if unknown:
        errs.append(f"{stype.name} has no field {', '.join(unknown)}")
    params = req.spec.get("params")
    if params is not None and not (isinstance(params, dict) and all(isinstance(v, str) for v in params.values())):
        errs.append("params must be key=value text")
    host = str(req.spec.get("host") or "")
    if host and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
        errs.append("host must be a host name, without http:// or a path")
    return errs


def clean_spec(req: SourceRequest, stype: SourceType) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in stype.fields:
        if f.side != "spec":
            continue
        v = req.spec.get(f.key, f.default)
        if v in (None, "", {}, []):
            continue
        out[f.key] = int(v) if f.kind == "int" else v
    preset = PRESETS.get(req.preset)
    if preset and preset.params and "params" in stype.spec_keys:
        out["params"] = {**preset.params, **(out.get("params") or {})}
    return out


def secret_manifest(req: SourceRequest, tenant: str) -> dict[str, Any]:
    """The Secret with the credentials. Only for sending to DataOS (the writer base64-encodes the values)."""
    data = {k: v.get_secret_value() for k, v in req.secret.items() if v.get_secret_value()}
    return {"name": req.secret_name, "version": "v2alpha", "type": "secret", "tenant": tenant, "layer": "user",
            "tags": ["dapp", SOURCE_TAG, f"dapp-source-{req.name}"],
            "description": f"Credentials of source {req.name} (dapp)", "spec": {"type": "key-value", "data": data}}


def depot_manifest(req: SourceRequest, stype: SourceType, tenant: str) -> dict[str, Any]:
    purposes = SQL_PURPOSES if stype.name in ("postgresql", "mysql", "redshift", "oracle", "jdbc", "presto", "snowflake") \
        else ("rw",)
    preset = PRESETS.get(req.preset)
    what = preset.title if preset else stype.name
    return {"name": req.name, "version": "v2alpha", "type": "depot", "tenant": tenant, "layer": "user",
            "tags": ["dapp", SOURCE_TAG, f"dapp-source-{req.name}"],
            "description": (req.description.strip() or f"{what} source, connected with dapp")[:240],
            "spec": {"type": stype.name, "spec": clean_spec(req, stype),
                     "secrets": [{"id": f"{tenant}:{req.secret_name}", "purpose": p} for p in purposes]}}


def redact(text: str, req: SourceRequest | None = None) -> str:
    """Remove credentials from a message before it is shown: the request's own values, and anything URL-shaped."""
    out = text or ""
    if req:
        for v in req.secret.values():
            s = v.get_secret_value()
            if s and len(s) >= 4:
                out = out.replace(s, "•••")
    out = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1•••@", out)
    return re.sub(r"(?i)(password|passwd|pwd|secret|token|key)(\s*[=:]\s*)[^\s,;&\"']+", r"\1\2•••", out)
