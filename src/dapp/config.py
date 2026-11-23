"""Resolve which DataOS instance, tenant and identity to talk to.

Reuses the dataos-ctl config so people log in once (``dataos-ctl login``) and both tools work:

  ~/.dataos/v2/dataos.context.yaml                      current context name
  ~/.dataos/v2/<context>/.dataos.tenancy.config.yaml    tenancy.dataos_fqdn / dataos_tenant
  ~/.dataos/v2/.<fqdn>.dataos.cred.config.yaml          user.id, user.token.{apikey,access_token}

Agents and pods run headless with environment variables instead (same names dataos-ctl reads):

  DATAOS_FQDN, DATAOS_TENANT_ID
  DATAOS_RUN_AS_USER + DATAOS_RUN_AS_APIKEY   (or USER_ID + APIKEY)
  DATAOS_ACCESS_TOKEN                         (a bearer token, e.g. forwarded by the dashboard)
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    pass


# This deployment's instance. Override with DAPP_FQDN / DAPP_TENANT (or `dapp setup --fqdn --tenant`).
DEFAULT_FQDN = "poliks.instance.dataos.cloud"
DEFAULT_TENANT = "engineering"


def token_seconds_left(token: str) -> float | None:
    """Seconds until a JWT access token expires (negative = already expired). None if it isn't a readable JWT.
    The signature is not checked: this is only to say "your login expired" instead of a bare 401."""
    try:
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp")
        return float(exp) - time.time() if exp else None
    except (IndexError, ValueError, TypeError):
        return None


def dapp_config_path() -> Path:
    return Path(os.environ.get("DAPP_CONFIG") or Path.home() / ".dapp" / "config.yaml")


def load_dapp_config() -> dict:
    """What `dapp setup` saved: fqdn, tenant, user, apikey. Empty if never set up."""
    return _read_yaml(dapp_config_path())


def save_dapp_config(*, fqdn: str, tenant: str, user: str, apikey: str) -> Path:
    """Write the file readable by this user only (it holds an API key)."""
    path = dapp_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    data = yaml.safe_dump({"fqdn": fqdn, "tenant": tenant, "user": user, "apikey": apikey}, sort_keys=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path


@dataclass
class Context:
    name: str
    fqdn: str
    tenant: str
    user_id: str | None = None
    apikey: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)

    @property
    def base_url(self) -> str:
        return f"https://{self.fqdn}"

    def auth_headers(self) -> dict[str, str]:
        if self.apikey:
            return {"apikey": self.apikey}
        if self.access_token:
            left = token_seconds_left(self.access_token)
            if left is not None and left <= 0:
                raise ConfigError(
                    f"Your DataOS login expired {max(1, round(-left / 60))} minutes ago. Run `dataos-ctl login` "
                    "(it renews the session, usually without a browser) and try again."
                )
            return {"Authorization": f"Bearer {self.access_token}"}
        raise ConfigError(
            "No DataOS credentials. Run `dataos-ctl login`, or set DATAOS_RUN_AS_USER and DATAOS_RUN_AS_APIKEY."
        )

    def with_tenant(self, tenant: str | None) -> Context:
        if not tenant or tenant == self.tenant:
            return self
        return Context(self.name, self.fqdn, tenant, self.user_id, self.apikey, self.access_token)


def config_dir() -> Path:
    return Path(os.environ.get("DATAOS_CONFIG_DIRECTORY") or Path.home() / ".dataos" / "v2")


def _read_yaml(path: Path) -> dict:
    try:
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _pick(d: dict, *keys: str) -> str | None:
    """Viper writes keys lower-cased; older files may be camel-cased."""
    for k in keys:
        if d.get(k):
            return str(d[k])
    return None


def current_context_name(cfg: Path | None = None) -> str | None:
    cfg = cfg or config_dir()
    data = _read_yaml(cfg / "dataos.context.yaml").get("contexts", {})
    return data.get("current_context_name")


def list_contexts(cfg: Path | None = None) -> list[str]:
    cfg = cfg or config_dir()
    data = _read_yaml(cfg / "dataos.context.yaml").get("contexts", {})
    names = [c["name"] for c in data.get("context_list", []) if c.get("name")]
    return names + (["dapp"] if load_dapp_config().get("apikey") else [])


def load_context(name: str | None = None, tenant: str | None = None, cfg: Path | None = None) -> Context:
    env = os.environ
    cfg = cfg or config_dir()

    fqdn = env.get("DATAOS_FQDN")
    env_tenant = env.get("DATAOS_TENANT_ID")
    asked = name or env.get("CONTEXT") or env.get("TENANT")
    ctx_name = asked or current_context_name(cfg) or "env"
    # dapp's own config (from `dapp setup`) is used when asked for by name ("dapp"), or when dataos-ctl has no
    # current context at all, e.g. on a laptop that only has dapp. A working dataos-ctl login always wins otherwise.
    saved = load_dapp_config()
    if saved.get("apikey") and (asked == "dapp" or (not asked and not current_context_name(cfg))):
        ctx_name = "dapp"
        fqdn = fqdn or saved.get("fqdn")
        env_tenant = env_tenant or saved.get("tenant")

    if not (fqdn and env_tenant):
        tenancy = _read_yaml(cfg / ctx_name / ".dataos.tenancy.config.yaml").get("tenancy", {})
        fqdn = fqdn or _pick(tenancy, "dataos_fqdn", "dataosfqdn", "DataOsFqdn")
        env_tenant = env_tenant or _pick(tenancy, "dataos_tenant", "dataostenant", "DataOsTenant")
    if not fqdn or not env_tenant:
        raise ConfigError(
            f"Could not resolve DataOS context '{ctx_name}'. Log in with `dataos-ctl login` and pick your tenant "
            "(`dataos-ctl tenant select`), or set DATAOS_FQDN and DATAOS_TENANT_ID, or run `dapp setup` with an API key."
        )

    user_id = env.get("DATAOS_RUN_AS_USER") or env.get("USER_ID")
    apikey = env.get("DATAOS_RUN_AS_APIKEY") or env.get("APIKEY")
    access_token = env.get("DATAOS_ACCESS_TOKEN")
    if not (apikey or access_token) and ctx_name == "dapp":
        user_id, apikey = user_id or saved.get("user"), saved.get("apikey")
    if not (apikey or access_token):
        user = _read_yaml(cfg / f".{fqdn}.dataos.cred.config.yaml").get("user", {})
        token = user.get("token", {}) or {}
        user_id = user_id or user.get("id")
        apikey = _pick(token, "apikey")
        access_token = _pick(token, "access_token")

    return Context(ctx_name, fqdn, tenant or env_tenant, user_id, apikey, access_token)
