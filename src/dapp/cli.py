"""dapp: deploy and manage applications on DataOS.

Every command takes --json for agents. Writes go through dataos-ctl (or the Poros API), reads through the
Poros/Heimdall APIs, so nothing here prompts or opens a browser.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .client import DataOS, DataOSError
from .config import ConfigError, current_context_name, list_contexts, load_context
from .ctl import WriteError
from .platform import Platform, PlatformError
from .spec import AppSpec, dump_yaml, load_spec, wire_manifest

HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__, context_settings=HELP_OPTIONS)
secret_app = typer.Typer(no_args_is_help=True, help="Manage an app's secrets (values are never shown).", context_settings=HELP_OPTIONS)
access_app = typer.Typer(no_args_is_help=True, help="Control who can use or manage an app.", context_settings=HELP_OPTIONS)
keys_app = typer.Typer(no_args_is_help=True, help="Your DataOS API keys.", context_settings=HELP_OPTIONS)
llm_key_app = typer.Typer(no_args_is_help=True, help="LLM gateway keys: the per-app key an app uses to call the LLM.", context_settings=HELP_OPTIONS)
app.add_typer(secret_app, name="secret")
app.add_typer(access_app, name="access")
app.add_typer(keys_app, name="keys")
app.add_typer(llm_key_app, name="llm-key")

console = Console()
err = Console(stderr=True)


class State:
    context: str | None = None
    tenant: str | None = None
    json: bool = False


state = State()


@app.callback()
def _global(
    context: str | None = typer.Option(None, "--context", "-c", envvar="DAPP_CONTEXT",
                                          help="dataos-ctl context to use (default: its current one)"),
    tenant: str | None = typer.Option(None, "--tenant", "-t", help="Override the context's tenant"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output (for agents)"),
) -> None:
    state.context, state.tenant, state.json = context, tenant, as_json


def _platform() -> Platform:
    ctx = load_context(state.context, state.tenant)
    return Platform(DataOS(ctx))


def _out(data: Any, render=None) -> None:
    if state.json:
        sys.stdout.write(json.dumps(data, indent=2, default=str) + "\n")
    elif render:
        render(data)
    elif isinstance(data, str):
        console.print(data)
    else:
        console.print_json(json.dumps(data, default=str))


def _fail(e: Exception) -> None:
    if state.json:
        sys.stdout.write(json.dumps({"error": str(e), "kind": type(e).__name__}) + "\n")
    else:
        err.print(f"[red]error:[/red] {e}")
    raise typer.Exit(1)


def _run(fn):
    try:
        return fn()
    except (ConfigError, DataOSError, PlatformError, WriteError, ValueError, FileNotFoundError) as e:
        _fail(e)


def _table(rows: list[dict], cols: list[str], title: str | None = None) -> None:
    t = Table(title=title, header_style="bold")
    for c in cols:
        t.add_column(c)
    for r in rows:
        t.add_row(*["" if r.get(c) is None else str(r.get(c)) for c in cols])
    console.print(t)


# ---------------------------------------------------------------- context
@app.command()
def context() -> None:
    """Show the DataOS instance, tenant and user dapp will act as."""
    def go():
        ctx = load_context(state.context, state.tenant)
        _out({"context": ctx.name, "fqdn": ctx.fqdn, "tenant": ctx.tenant, "user": ctx.user_id,
              "auth": "apikey" if ctx.apikey else ("token" if ctx.access_token else "none"),
              "ctl_current_context": current_context_name(), "available": list_contexts()})
    _run(go)


@app.command()
def setup(
    fqdn: str | None = typer.Option(None, "--fqdn", envvar="DAPP_FQDN", help="DataOS instance host"),
    tenant: str | None = typer.Option(None, "--tenant", envvar="DAPP_TENANT"),
    api_key_stdin: bool = typer.Option(False, "--api-key-stdin", help="Read the API key from stdin"),
) -> None:
    """Save the DataOS instance, tenant and your API key (checked first) so dapp works without dataos-ctl.

    Get a key in the dashboard: Keys page -> Create API key. It is stored in ~/.dapp/config.yaml, readable by you
    only. Non-interactive: set DAPP_SETUP_APIKEY (or pipe it with --api-key-stdin), never put it on a command line.
    """
    from .auth import Heimdall, IdentityUnavailable
    from .config import DEFAULT_FQDN, DEFAULT_TENANT, save_dapp_config

    def go():
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        host = fqdn or (typer.prompt("DataOS instance", default=DEFAULT_FQDN) if interactive else DEFAULT_FQDN)
        ten = tenant or (typer.prompt("Tenant", default=DEFAULT_TENANT) if interactive else DEFAULT_TENANT)
        host = host.removeprefix("https://").rstrip("/")
        key = (sys.stdin.read().strip() if api_key_stdin else os.environ.get("DAPP_SETUP_APIKEY", "").strip()
               or (typer.prompt("DataOS API key (input hidden)", hide_input=True) if interactive else ""))
        if not key:
            raise PlatformError("no API key: create one in the dashboard (Keys page), then set DAPP_SETUP_APIKEY "
                                "or run `dapp setup` in a terminal")
        try:
            ident = Heimdall(f"https://{host}").identify(key)
        except IdentityUnavailable as e:
            raise PlatformError(f"could not reach https://{host}: {e}") from e
        if ident is None:
            raise PlatformError(f"https://{host} did not accept that API key (expired, mistyped, or from another instance)")
        path = save_dapp_config(fqdn=host, tenant=ten, user=ident.user_id, apikey=key)
        _out({"user": ident.user_id, "fqdn": host, "tenant": ten, "saved": str(path)},
             lambda r: console.print(f"[green]ready[/green]: {r['user']} on {r['fqdn']} (tenant {r['tenant']}), saved to {r['saved']}\n"
                                     "next: `dapp doctor`"))
    _run(go)


@app.command()
def doctor() -> None:
    """Check this machine is ready to deploy: credentials, access, compute, gateway. Exits 1 if a required check fails."""
    from .doctor import run_checks

    def go():
        checks = run_checks(state.context, state.tenant)
        failed = [c for c in checks if c["required"] and not c["ok"]]

        def show(rows):
            for c in rows:
                mark = "[green]ok[/green]  " if c["ok"] else ("[red]FAIL[/red]" if c["required"] else "[yellow]warn[/yellow]")
                console.print(f"{mark} {c['check']:15s} {c['detail']}")
                if not c["ok"] and c.get("hint"):
                    console.print(f"       [dim]{c['hint']}[/dim]")
        _out({"ok": not failed, "checks": checks} if state.json else checks, show)
        if failed:
            raise typer.Exit(1)
    _run(go)


@app.command()
def version() -> None:
    """Print the dapp version."""
    _out({"dapp": __version__} if state.json else f"dapp {__version__}")


# ---------------------------------------------------------------- scaffold
TEMPLATE = """\
# dapp.yaml: deploy with `dapp deploy`
name: {name}
description: {description}
image: {image}
port: {port}
command: [/docker-entrypoint.sh]   # required: the image entrypoint
args: [nginx, -g, "daemon off;"]
compute: {compute}          # a DataOS compute resource you may `use`
# runAs: my-bot-user        # run as another DataOS user (needs a run-as grant)
replicas: 1
resources: {{cpu: 100m, memory: 128Mi, cpuLimit: "1", memoryLimit: 512Mi}}
# autoscale: {{min: 1, max: 3, cpu: 80}}
env:
  LOG_LEVEL: info
secrets: []                 # existing DataOS secrets to inject as env vars
llm: false                  # true = route Anthropic/OpenAI calls via the dapp gateway and meter tokens
public: false               # true = skip DataOS login in front of the app
# path: /{name}             # default /<name>
"""


CHATBOT_IMAGE = "docker.io/tmdcio/dapp-chatbot:0.1.6"   # public image built from examples/chatbot
CHATBOT_TEMPLATE = """\
# A ready LLM chatbot: private (DataOS sign-in), calls go through the dapp gateway so tokens and cost are counted
# per person. Nothing to build. Deploy with:  dapp deploy
name: {name}
title: {name}
description: Chatbot {name}
image: {image}
port: 8080
command: [sh, -c, "uvicorn app:app --host 0.0.0.0 --port 8080 --proxy-headers"]
compute: {compute}
path: /{name}
resources: {{cpu: 100m, memory: 192Mi, cpuLimit: 500m, memoryLimit: 384Mi}}
llm: true
metrics: true
env:
  ROOT_PATH: /{name}          # must equal path
  OPENAI_MODEL: gpt-6-luna
  OPENAI_REASONING_EFFORT: low
"""


def compute_or_default(compute: str) -> str:
    """`--compute` wins; otherwise DAPP_DEFAULT_COMPUTE, otherwise this deployment's compute."""
    return compute if compute != "runnable-default" else os.environ.get("DAPP_DEFAULT_COMPUTE", "engineering-compute")


@app.command()
def init(
    name: str = typer.Argument(..., help="App name (lowercase, digits, '-')"),
    image: str = typer.Option("nginxinc/nginx-unprivileged:1.27-alpine", help="Container image"),
    port: int = typer.Option(8080),
    compute: str = typer.Option("runnable-default", help="DataOS compute resource"),
    description: str = typer.Option("My DataOS app"),
    file: Path = typer.Option(Path("dapp.yaml"), "--file", "-f"),
    force: bool = typer.Option(False, help="Overwrite an existing file"),
    template: str = typer.Option("blank", "--template", help="blank | chatbot (a ready OpenAI chatbot, no build needed)"),
) -> None:
    """Write a starter dapp.yaml. `--template chatbot` gives a working LLM chatbot you can deploy as is."""
    def go():
        if file.exists() and not force:
            raise PlatformError(f"{file} exists (use --force)")
        if template == "chatbot":
            text = CHATBOT_TEMPLATE.format(name=name, image=CHATBOT_IMAGE, compute=compute_or_default(compute))
        elif template == "blank":
            text = TEMPLATE.format(name=name, image=image, port=port, compute=compute, description=description)
        else:
            raise PlatformError(f"unknown template '{template}' (blank or chatbot)")
        AppSpec.model_validate(__import__("yaml").safe_load(text))  # template must stay valid
        file.write_text(text)
        _out({"written": str(file)} if state.json else f"wrote {file}")
    _run(go)


# ---------------------------------------------------------------- deploy
@app.command()
def deploy(
    file: Path = typer.Option(Path("dapp.yaml"), "--file", "-f", help="App spec"),
    compute: str | None = typer.Option(None, help="Compute to run on (overrides dapp.yaml)"),
    lint: bool = typer.Option(False, help="Validate with dataos-ctl --lint; don't deploy"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the generated DataOS manifest only"),
    rotate_llm_key: bool = typer.Option(False, help="Issue a new LLM gateway key for the app"),
) -> None:
    """Create or update an app from dapp.yaml."""
    def go():
        spec = load_spec(file)
        p = _platform()
        if dry_run:
            plan = p.plan(spec, compute=compute)
            wire = [wire_manifest(m) for m in plan.manifests]  # exactly what would be submitted
            _out({"manifests": wire, "notes": plan.notes} if state.json else dump_yaml(*wire))
            return
        res = p.deploy(spec, compute=compute, lint=lint, rotate_llm_key=rotate_llm_key)

        def show(r):
            console.print(r["output"])
            for n in r["notes"]:
                console.print(f"[dim]- {n}[/dim]")
            if not r["linted_only"]:
                console.print(f"[green]deployed[/green] {r['app']} -> {r['url']}  (check: dapp status {r['app']})")
        _out(res, show)
    _run(go)


@app.command("ls")
def list_apps(everyone: bool = typer.Option(False, "--all", "-a", help="All apps in the tenant, not only mine")):
    """List apps (yours by default)."""
    _run(lambda: _out(_platform().list_apps(everyone=everyone),
                      lambda rows: _table(rows, ["name", "state", "runtime", "owner", "url", "error"])))


@app.command()
def status(name: str) -> None:
    """App status, pods and recent events."""
    def show(s):
        _table([s], ["name", "state", "runtime", "owner", "url", "error"])
        rd = s.get("runtime_detail") or {}
        nodes = rd.get("nodeDetails") or rd.get("nodes") or []
        if nodes:
            _table([{"pod": n.get("nodeName") or n.get("name"), "phase": n.get("phase") or n.get("status"),
                     "containers": ",".join(n.get("containers") or []), "dataplane": n.get("dataplane")}
                    for n in nodes],
                   ["pod", "phase", "containers", "dataplane"], title="pods")
        elif rd.get("error"):
            console.print(f"[yellow]runtime: {rd['error']}[/yellow]")
    _run(lambda: _out(_platform().status(name), show))


@app.command()
def logs(name: str, tail: int = typer.Option(100, "--tail", "-n"),
         pod: str | None = typer.Option(None, help="Pod (node) name")) -> None:
    """Recent logs of the app's containers."""
    p = lambda: _platform()
    _run(lambda: _out(p().logs(name, tail=tail, node=pod)) if state.json
         else console.print(p().logs_text(name, tail=tail, node=pod), markup=False, highlight=False))


@app.command()
def scale(name: str, replicas: int) -> None:
    """Set the number of replicas."""
    _run(lambda: _out(_platform().scale(name, replicas), lambda r: console.print(r["output"])))


@app.command("rm")
def remove(name: str,
           purge: bool = typer.Option(False, help="Also delete the app's dapp secrets, grants and LLM keys"),
           yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)")) -> None:
    """Delete an app."""
    def go():
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to delete without --yes")
            typer.confirm(f"Delete app {name}{' and its secrets/grants/keys' if purge else ''}?", abort=True)
        _out(_platform().delete(name, purge=purge), lambda r: console.print(r["output"]))
    _run(go)


# ---------------------------------------------------------------- secrets
def _parse_pairs(pairs: list[str], env_file: Path | None) -> dict[str, str]:
    data: dict[str, str] = {}
    if env_file:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    for p in pairs:
        if "=" not in p:
            raise PlatformError(f"expected KEY=VALUE, got '{p}'")
        k, v = p.split("=", 1)
        data[k] = v
    return data


@secret_app.command("ls")
def secret_ls(app_name: str = typer.Argument(..., metavar="APP")) -> None:
    """Secrets an app uses."""
    _run(lambda: _out(_platform().list_secrets(app_name),
                      lambda rows: _table(rows, ["name", "managed_by_dapp", "referenced", "owner"])))


@secret_app.command("set")
def secret_set(
    app_name: str = typer.Argument(..., metavar="APP"),
    secret: str = typer.Argument(..., help="Secret name"),
    pairs: list[str] = typer.Argument(None, help="KEY=VALUE ... (replaces all keys)"),
    env_file: Path | None = typer.Option(None, "--from-env-file", help="Read KEY=VALUE lines from a file"),
    no_attach: bool = typer.Option(False, help="Create the secret but don't inject it into the app"),
) -> None:
    """Create/replace a secret and inject it into the app as env vars."""
    _run(lambda: _out(_platform().set_secret(app_name, secret, _parse_pairs(pairs or [], env_file),
                                             attach=not no_attach),
                      lambda r: console.print(f"set {r['secret']} keys={r['keys']}\n{r['output']}")))


@secret_app.command("apply")
def secret_apply(
    file: Path = typer.Argument(..., help="Secret YAML whose values are ${VAR} references"),
    env_file: list[Path] = typer.Option(None, "--env-file", help="KEY=VALUE file to read the variables from; repeatable"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Check the variables and show key names; apply nothing"),
) -> None:
    """Apply a secret YAML (see deploy/secrets/), filling ${VAR} from the environment and --env-file.

    Refuses unset variables and placeholder values. Prints secret and key names, never values.
    """
    def go():
        env = dict(os.environ)
        for f in env_file or []:
            env.update(_parse_pairs([], f))
        r = _platform().apply_secret_file(str(file), env, dry_run=dry_run)

        def show(r):
            for s in r["secrets"]:
                console.print(f"{'would apply' if not r['applied'] else 'applied'} secret {s['name']} "
                              f"(tenant {s['tenant']}) keys={s['keys']}")
        _out(r, show)
    _run(go)


@secret_app.command("rm")
def secret_rm(app_name: str = typer.Argument(..., metavar="APP"), secret: str = typer.Argument(...)) -> None:
    """Detach and delete a secret."""
    _run(lambda: _out(_platform().delete_secret(app_name, secret), lambda r: console.print(r["output"])))


# ---------------------------------------------------------------- access
@access_app.command("ls")
def access_ls(app_name: str = typer.Argument(..., metavar="APP")) -> None:
    """Who can open an app (owners, allowed users and roles)."""
    def show(a):
        console.print(f"[bold]{a['app']}[/bold]  mode: {a['mode']}")
        console.print(f"owners: {', '.join(a['owners']) or '-'}")
        console.print(f"users:  {', '.join(a['users']) or '-'}")
        console.print(f"roles:  {', '.join(a['roles']) or '-'}")
    _run(lambda: _out(_platform().allowlist(app_name), show))


def _show_allow(r):
    for typed, uid in (r.get("resolved") or {}).items():
        console.print(f"[dim]{typed} is {uid}[/dim]")
    console.print(f"users: {', '.join(r['users']) or '-'}\nroles: {', '.join(r['roles']) or '-'}\n[dim]{r['note']}[/dim]")


@access_app.command("allow")
def access_allow(
    app_name: str = typer.Argument(..., metavar="APP"),
    users: list[str] = typer.Argument(None, help='DataOS user ids, or "*" for any DataOS user'),
    role: list[str] = typer.Option(None, "--role", help="Heimdall role id, e.g. engineering-tenant-admin"),
) -> None:
    """Let users (or everyone with a role) open the app."""
    _run(lambda: _out(_platform().allow(app_name, users or [], role or []), _show_allow))


@access_app.command("deny")
def access_deny(
    app_name: str = typer.Argument(..., metavar="APP"),
    users: list[str] = typer.Argument(None),
    role: list[str] = typer.Option(None, "--role"),
) -> None:
    """Remove users or roles from the app's allow-list."""
    _run(lambda: _out(_platform().deny(app_name, users or [], role or []), _show_allow))


@access_app.command("grants")
def access_grants(app_name: str = typer.Argument(..., metavar="APP")) -> None:
    """Heimdall grants dapp created for an app (advanced)."""
    _run(lambda: _out(_platform().list_access(app_name),
                      lambda rows: _table(rows, ["name", "use_case", "subjects", "status"])))


@access_app.command("grant")
def access_grant(
    app_name: str = typer.Argument(..., metavar="APP"),
    user: str = typer.Argument(..., help="DataOS user id, or a full subject like roles:id:data-dev"),
    use_case: str = typer.Option(..., "--use-case", help="Heimdall policy use case id"),
    value: list[str] = typer.Option(None, "--value", help="ATOM:KEY=VAL, repeatable (use-case values)"),
) -> None:
    """Advanced: grant a Heimdall policy use case, recorded against the app."""
    def go():
        values: dict[str, list[dict[str, str]]] = {}
        for v in value or []:
            atom, _, kv = v.partition(":")
            k, _, val = kv.partition("=")
            if not (atom and k):
                raise PlatformError(f"bad --value '{v}', expected ATOM:KEY=VAL")
            values.setdefault(atom, []).append({k: val})
        _out(_platform().grant_access(app_name, user, use_case=use_case, values=values),
             lambda r: console.print(f"grant {r['grant']}\n{r['output']}"))
    _run(go)


@access_app.command("revoke")
def access_revoke(grant: str) -> None:
    """Delete a grant by name (see `dapp access ls`)."""
    _run(lambda: _out(_platform().revoke_access(grant), lambda r: console.print(r["output"])))


# ---------------------------------------------------------------- keys
@keys_app.command("ls")
def keys_ls() -> None:
    """Your DataOS API keys (key material is never shown)."""
    _run(lambda: _out(_platform().list_keys(),
                      lambda rows: _table(rows, ["name", "type", "expiration", "description"])))


@keys_app.command("create")
def keys_create(name: str, duration: str = typer.Option("720h"), description: str = typer.Option("")) -> None:
    """Create a DataOS API key (printed once)."""
    _run(lambda: _out(_platform().create_key(name, duration, description)))


@keys_app.command("rm")
def keys_rm(name: str, force: bool = typer.Option(False, help="Also delete a key DataOS created for your apps")) -> None:
    """Delete a DataOS API key."""
    _run(lambda: (_platform().delete_key(name, force=force), _out({"deleted": name}))[1])


# ---------------------------------------------------------------- llm keys
@llm_key_app.command("ls")
def llm_key_ls() -> None:
    """Gateway keys of your apps. Only the prefix is shown: the gateway keeps a hash, not the key."""
    _run(lambda: _out(_platform().llm_keys(),
                      lambda rows: _table(rows, ["app", "tenant", "prefix", "created", "revoked"])))


@llm_key_app.command("rotate")
def llm_key_rotate(
    app_name: str = typer.Argument(..., metavar="APP"),
    timeout: int = typer.Option(150, help="Seconds to wait for the app to restart before revoking the old key"),
) -> None:
    """New key into the app's secret, DataOS restarts the app, then the old keys are revoked (no failed calls)."""
    def go():
        err.print("issuing a new key and waiting for the app to restart (about 40s)...") if not state.json else None
        r = _platform().rotate_llm_key(app_name, timeout=timeout)

        def show(r):
            console.print(f"new key {r['new_key_prefix']}…  stored in secret {r['secret']}")
            if r["rolled_over"]:
                console.print(f"app restarted; revoked old keys: {', '.join(r['revoked']) or 'none were active'}")
            else:
                console.print(f"[yellow]app didn't restart within {timeout}s; old keys still active: "
                              f"{', '.join(r['still_active'])}. Revoke them with `dapp llm-key revoke`.[/yellow]")
        _out(r, show)
    _run(go)


@llm_key_app.command("revoke")
def llm_key_revoke(
    app_name: str = typer.Argument(..., metavar="APP"),
    prefix: str = typer.Argument(..., help="Key prefix from `dapp llm-key ls`, e.g. dapp_ID3bA8G"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)"),
) -> None:
    """Kill one key now. If the app is still using it, its LLM calls fail until you rotate."""
    def go():
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to revoke without --yes")
            typer.confirm(f"Revoke {prefix}…? If {app_name} still uses it, its LLM calls fail until you rotate.",
                          abort=True)
        _out(_platform().revoke_llm_key(app_name, prefix), lambda r: console.print(f"revoked {r['prefix']}…"))
    _run(go)


# ---------------------------------------------------------------- usage
@app.command()
def usage(app_name: str | None = typer.Argument(None, metavar="[APP]"),
          hours: int = typer.Option(24 * 30, help="LLM usage window"),
          by: str = typer.Option("app,model", "--by", help="Group by: app, model, day, end_user (comma list). "
                                 "`--by end_user` shows who used the app")) -> None:
    """LLM token usage, and compute for one app. `dapp usage chatbot --by end_user` = usage per person."""
    def go():
        p = _platform()
        out: dict[str, Any] = {}
        try:
            out["llm"] = p.llm_usage(app_name, since_hours=hours, group_by=by)
        except Exception as e:
            out["llm"] = {"error": str(e)}
        if app_name:
            try:
                out["compute"] = p.compute_usage(app_name)
            except Exception as e:
                out["compute"] = {"error": str(e)}
        _out(out)
    _run(go)


# ---------------------------------------------------------------- mcp
@app.command()
def mcp() -> None:
    """Run dapp as an MCP server over stdio (for Claude Code and other agents)."""
    from .mcp_server import run

    run(context=state.context, tenant=state.tenant)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
