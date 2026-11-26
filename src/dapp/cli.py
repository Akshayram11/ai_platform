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
from pydantic import SecretStr
from rich.console import Console
from rich.table import Table

from . import __version__
from .claude import CLAUDE_CODE_IMAGE
from .client import DataOS, DataOSError
from .config import ConfigError, current_context_name, list_contexts, load_context
from .ctl import WriteError
from .pipelines import HOWS, PipelineRequest
from .pipelines import summary as pipeline_summary
from .platform import PipelineAccessError, Platform, PlatformError
from .sources import PRESETS, SourceRequest, from_url
from .spec import AppSpec, dump_yaml, load_spec, wire_manifest
from .usage import GatewayError

HELP_OPTIONS = {"help_option_names": ["-h", "--help"]}

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__, context_settings=HELP_OPTIONS)
secret_app = typer.Typer(no_args_is_help=True, help="Manage an app's secrets (values are never shown).", context_settings=HELP_OPTIONS)
access_app = typer.Typer(no_args_is_help=True, help="Control who can use or manage an app.", context_settings=HELP_OPTIONS)
keys_app = typer.Typer(no_args_is_help=True, help="Your DataOS API keys.", context_settings=HELP_OPTIONS)
llm_key_app = typer.Typer(no_args_is_help=True, help="LLM gateway keys: the per-app key an app uses to call the LLM.", context_settings=HELP_OPTIONS)
tool_app = typer.Typer(no_args_is_help=True, help="MCP tools through the gateway: register, share, connect, usage.", context_settings=HELP_OPTIONS)
model_app = typer.Typer(no_args_is_help=True, context_settings=HELP_OPTIONS,
                        help="Small language models on DataOS CPUs: catalog, deploy, list, delete (the dashboard's Models tab).")
app.add_typer(secret_app, name="secret")
app.add_typer(access_app, name="access")
app.add_typer(keys_app, name="keys")
app.add_typer(llm_key_app, name="llm-key")
app.add_typer(tool_app, name="tool")
app.add_typer(model_app, name="model")
pipeline_app = typer.Typer(no_args_is_help=True, context_settings=HELP_OPTIONS,
                           help="Move data with DataOS Nilus in plain words: Depots, new, ls, status, logs, run, rm "
                                "(the dashboard's Pipelines tab).")
app.add_typer(pipeline_app, name="pipeline")
source_app = typer.Typer(no_args_is_help=True, context_settings=HELP_OPTIONS,
                         help="Connect a new source (a Secret with its credentials and a Depot on top), list, replace "
                              "credentials, remove. Credentials come from environment variables or files, never arguments.")
app.add_typer(source_app, name="source")

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
    except (ConfigError, DataOSError, PlatformError, GatewayError, WriteError, ValueError, FileNotFoundError) as e:
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


CHATBOT_IMAGE = "docker.io/tmdcio/dapp-chatbot:0.1.14"   # public image built from examples/chatbot
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
objects: true                 # each signed-in person's chats are kept in immortal-obj and survive restarts
metrics: true
env:
  ROOT_PATH: /{name}          # must equal path
  OPENAI_MODEL: {model}       # the chat's default; every model deployed with `dapp model deploy` is also in its drop-down
{reasoning}  # SYSTEM_PROMPT: You are a friendly assistant for our team.
"""


CLAUDE_CODE_TEMPLATE = """\
# Your own Claude Code on DataOS: a browser terminal to build and deploy apps by asking. Only you (the owner) can
# open it, and it deploys as you. Sign in with your Claude account inside the terminal. Deploy with:  dapp deploy
name: {name}
title: Claude Code ({name})
description: Claude Code in a browser terminal; builds and deploys DataOS apps with dapp
image: {image}
command: [/opt/claude/start.sh]
port: 8080
compute: {compute}
path: /{name}
metrics: true
resources: {{cpu: 250m, memory: 1Gi, cpuLimit: "2", memoryLimit: 2Gi}}
env:
  ROOT_PATH: /{name}          # must equal path
  DATAOS_FQDN: {fqdn}
  DAPP_DEFAULT_COMPUTE: {compute}
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
    template: str = typer.Option("blank", "--template", help="blank | chatbot (a ready OpenAI chatbot) | claude-code (your own Claude Code terminal)"),
    model: str = typer.Option("gpt-6-luna", "--model", help="chatbot template: the model it answers with. Use the name of a model "
                                                            "from `dapp model ls` for a low-cost chatbot that runs inside DataOS"),
) -> None:
    """Write a starter dapp.yaml. `--template chatbot` gives a working LLM chatbot you can deploy as is."""
    def go():
        if file.exists() and not force:
            raise PlatformError(f"{file} exists (use --force)")
        if template == "chatbot":
            # reasoning effort is an OpenAI setting: only for their models, not for one running inside DataOS
            reasoning = "  OPENAI_REASONING_EFFORT: low     # gpt-6-luna reasons by default; low keeps a chat fast and cheap\n" \
                if model.startswith("gpt") else ""
            text = CHATBOT_TEMPLATE.format(name=name, image=CHATBOT_IMAGE, compute=compute_or_default(compute), model=model,
                                           reasoning=reasoning)
        elif template == "claude-code":
            try:  # the instance you're logged in to; the default one if there's no login yet
                fqdn = load_context(state.context, state.tenant).fqdn
            except ConfigError:
                from .config import DEFAULT_FQDN as fqdn
            text = CLAUDE_CODE_TEMPLATE.format(name=name, image=CLAUDE_CODE_IMAGE, compute=compute_or_default(compute),
                                               fqdn=fqdn)
        elif template == "blank":
            text = TEMPLATE.format(name=name, image=image, port=port, compute=compute, description=description)
        else:
            raise PlatformError(f"unknown template '{template}' (blank, chatbot or claude-code)")
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
def logs(name: str, tail: int = typer.Option(100, "--tail", "-n", help="Lines (the running pod), or results (with a search)"),
         pod: str | None = typer.Option(None, help="Pod (node) name"),
         search: str | None = typer.Option(None, "--search", "-s", help="Search stored logs for a keyword (* for all)"),
         since: str | None = typer.Option(None, "--since", help="Stored logs of the last 30m, 1h, 24h, 7d ..."),
         start: str | None = typer.Option(None, "--from", help="Stored logs from this time: 2026-11-26T16:00:00Z, '2h ago'"),
         end: str | None = typer.Option(None, "--to", help="... to this time (default now)"),
         stream: str | None = typer.Option(None, "--stream", help="stdout or stderr (stored logs)"),
         kind: str = typer.Option("app", "--kind", help="Resource type for stored logs: app, nilus, service, workflow")) -> None:
    """Logs of an app. Plain: the running pod's last lines. With --search/--since/--from: DataOS's stored logs over any time
    range, crashed and replaced pods included (e.g. `dapp logs chatbot --search error --since 24h`)."""
    p = lambda: _platform()
    if any(v is not None for v in (search, since, start, end, stream)) or kind != "app":
        def go():
            r = p().search_logs(name, kind=kind, query=search or "*", since=since, start=start, end=end, pod=pod,
                                stream=stream, limit=min(max(tail, 1), 1000))
            _out(r, lambda x: [console.print(f"{l['time']}  {l['stream'] or '':6} {(l['pod'] or '')[-12:]:12} {l['message']}",
                                             markup=False, highlight=False) for l in x["lines"]]
                 or console.print(f"[dim]{x['returned']} of {x['total']} lines[/dim]"))
        _run(go)
        return
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


@llm_key_app.command("personal")
def llm_key_personal(
    save: Path = typer.Option(None, "--save", help="Write DAPP_KEY and DAPP_BASE_URL to this file (mode 600) "
                                                   "instead of printing the key"),
    rotate: bool = typer.Option(False, "--rotate", help="Revoke your earlier personal keys"),
) -> None:
    """Your own LLM key, to call the gateway's models (e.g. "small") from your laptop. Counted as app user:<you>."""
    def go():
        r = _platform().personal_llm_key(rotate=rotate)
        if save:
            target = save.expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(mode=0o600, exist_ok=True)
            target.chmod(0o600)
            target.write_text(f"DAPP_KEY={r['key']}\nDAPP_BASE_URL={r['base_url']}\n")
            _out({"app": r["app"], "prefix": r["prefix"], "saved": str(target), "base_url": r["base_url"]},
                 lambda x: console.print(f"key {x['prefix']}… saved to {x['saved']}\n"
                                         f"use it: set -a; . {x['saved']}; set +a   (then \"$DAPP_KEY\" and \"$DAPP_BASE_URL\")"))
        else:
            _out(r, lambda x: console.print(f"DAPP_KEY={x['key']}\nDAPP_BASE_URL={x['base_url']}\n"
                                            "[dim]shown once: the gateway keeps only a hash[/dim]"))
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


# ---------------------------------------------------------------- models
def _model_cost(d_or_est) -> str:
    cur = d_or_est["currency"]
    return f"{cur}{d_or_est['day']:.2f}/day, {cur}{d_or_est['month']:.0f}/month"


@model_app.command("catalog")
def model_catalog() -> None:
    """The models you can deploy, with quality, speed, what they need and an estimate of what keeping them running costs."""
    from .models import CATALOG, defaults_for, estimate, quality_of, speed_of, tier_of, traits_of

    rows = []
    for m in CATALOG:
        d = defaults_for(m)
        e = estimate(cpu=d["cpu"], cpu_limit=d["cpu_limit"], memory=d["memory"], memory_limit=d["memory_limit"],
                     disk_gi=d["disk_gi"], size_gb=m.size_gb)
        rows.append({"id": m.id, "title": m.title, "params": m.params, "tier": tier_of(m), "quality": quality_of(m),
                     "speed": speed_of(m.size_gb)[1], "words_per_s": round(e["words_per_s"]), "memory": d["memory_limit"],
                     "cpu": d["cpu_limit"], "download_gb": m.size_gb, "cost_day": round(e["day"], 2), "cost_month": round(e["month"]),
                     "licence": m.license, "good_for": ", ".join(traits_of(m)), "huggingface": m.hf})

    def show(r):
        _table([{"id": x["id"], "params": x["params"], "quality": f"{x['quality']}/5", "words/s": x["words_per_s"],
                 "memory": x["memory"], "cpu": x["cpu"], "GB": x["download_gb"], "$/day": f"{x['cost_day']:.2f}"} for x in r],
               ["id", "params", "quality", "words/s", "memory", "cpu", "GB", "$/day"], "Deploy with: dapp model deploy ID")
        console.print("[dim]Quality is a rough guide (5 best), not a benchmark. $/day is an estimate from list prices (DAPP_COST_*) "
                      "for comparing models; DataOS doesn't bill these apps. `dapp --json model catalog` has everything.[/dim]")
    _out(rows, show)


@model_app.command("ls")
def model_ls() -> None:
    """Models you can call in this tenant, whether each is ready, and who deployed it."""
    def show(rows):
        _table([{"model": m["name"], "state": "ready" if m.get("ready") else "starting", "weights": m.get("repo") or "-",
                 "app": m.get("app"), "owner": m.get("owner") or "(gateway config)"} for m in rows],
               ["model", "state", "weights", "app", "owner"])
    _run(lambda: _out(_platform().models(), show))


def _wait_ready(p: Platform, name: str, timeout: int) -> bool:
    import time

    deadline, shown = time.monotonic() + timeout, False
    while time.monotonic() < deadline:
        if any(m["name"] == name and m.get("ready") for m in p.models()):
            return True
        if not state.json:
            err.print("[dim].[/dim]", end="")
            shown = True
        time.sleep(5)
    if shown:
        err.print()
    return False


@model_app.command("deploy")
def model_deploy(
    model: str = typer.Argument(None, metavar="[MODEL]", help="A catalog id, e.g. qwen2.5-1.5b (see `dapp model catalog`)"),
    hf: str = typer.Option(None, "--hf", help="A custom Hugging Face GGUF repo instead: owner/repo[:Quant]"),
    name: str = typer.Option(None, "--name", help="What callers put in `model` (default: the catalog id)"),
    quant: str = typer.Option(None, "--quant", help="Quantization, e.g. Q4_K_M, Q5_K_M, Q8_0"),
    compute: str = typer.Option(None, "--compute", help="Compute resource (default: DAPP_DEFAULT_COMPUTE or engineering-compute)"),
    cpu: str = typer.Option(None, "--cpu", help="CPU reserved, e.g. 500m or 1"),
    cpu_limit: str = typer.Option(None, "--cpu-limit", help="CPU limit in cores (8 at most)"),
    memory: str = typer.Option(None, "--memory", help="Memory reserved, e.g. 1Gi"),
    memory_limit: str = typer.Option(None, "--memory-limit", help="Memory limit, e.g. 2Gi"),
    disk: int = typer.Option(None, "--disk", help="Volume size in Gi for the downloaded model"),
    setting: list[str] = typer.Option(None, "--set", help="Any other setting as key=value, repeatable: ctx, parallel, threads, batch, "
                                      "ubatch, flash_attn, cache_type_k, cache_type_v, max_tokens, reasoning, jinja, context_shift, "
                                      "cont_batching, timeout_s, temperature, top_k, top_p, min_p, repeat_penalty"),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until the model has loaded and answers"),
    timeout: int = typer.Option(900, help="Seconds to wait for it to load"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the settings and the cost estimate; deploy nothing"),
) -> None:
    """Deploy a small model: a Volume, a key, a private llama.cpp app, and its name in the gateway. Defaults fit the model;
    every setting of the dashboard's Customize menu is a flag or a --set. The model downloads on its first start."""
    from .models import build_deploy, estimate

    def go():
        extra: dict[str, str] = {}
        for item in setting or []:
            if "=" not in item:
                raise ValueError(f"--set {item!r}: use key=value, e.g. --set ctx=8192")
            k, v = item.split("=", 1)
            extra[k.strip().replace("-", "_")] = v.strip()
        d = build_deploy(model_id=model or "", hf=hf or "", name=name or "", overrides={
            "quant": quant, "compute": compute, "cpu": cpu, "cpu_limit": cpu_limit, "memory": memory,
            "memory_limit": memory_limit, "disk_gi": disk, **extra})
        e = estimate(cpu=d.cpu, cpu_limit=d.cpu_limit, memory=d.memory, memory_limit=d.memory_limit, disk_gi=d.disk_gi,
                     size_gb=d.size_gb)
        plan = {"name": d.name, "weights": d.source, "app": d.app, "cpu": f"{d.cpu} (up to {d.cpu_limit})",
                "memory": f"{d.memory} (up to {d.memory_limit})", "disk": f"{d.disk_gi}Gi", "context": d.ctx,
                "estimated_cost": _model_cost(e), "speed": f"about {e['words_per_s']:.0f} words/s", "server_env": d.server_env()}
        if dry_run:
            _out(plan, lambda x: (console.print_json(json.dumps(x, default=str)),
                                  console.print("[dim]dry run: nothing deployed[/dim]")))
            return
        p = _platform()
        target = compute or d.compute or os.environ.get("DAPP_DEFAULT_COMPUTE") or "engineering-compute"
        done = p.deploy_model(d, compute=target, dataplane=os.environ.get("DAPP_MODELS_DATAPLANE", "poldp01"))
        ready = _wait_ready(p, d.name, timeout) if wait and not state.json else None
        result = {**done, "estimated_cost": plan["estimated_cost"], "ready": ready}

        def show(r):
            console.print(f"deployed model [bold]{r['name']}[/bold] ({r['source']}) as app {r['app']}; "
                          f"estimated cost {r['estimated_cost']}")
            console.print("it is [green]ready[/green]" if r["ready"] else
                          "it is starting: it downloads and loads the model (a minute or two). Check with `dapp model ls`.")
            console.print(f"use it: dapp llm-key personal --save ~/.dapp/llm.env, then POST $DAPP_BASE_URL/chat/completions "
                          f"with \"model\": \"{r['name']}\"; or `llm: true` in an app's dapp.yaml")
        _out(result, show)
    _run(go)


@model_app.command("rm")
def model_rm(
    name: str = typer.Argument(..., metavar="MODEL"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)"),
    keep_volume: bool = typer.Option(False, "--keep-volume", help="Leave the Volume with the downloaded model"),
) -> None:
    """Delete a model you deployed: the gateway stops routing to it, then its app and key go, then its Volume."""
    def go():
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to delete without --yes")
            typer.confirm(f"Delete model {name}? Calls to it stop working.", abort=True)
        p = _platform()
        gone = p.delete_model(name)
        freed = None if keep_volume else p.delete_model_volume(gone["volume"], timeout=240)
        _out({**gone, "volume_deleted": freed}, lambda r: console.print(
            f"deleted model {r['name']} (app {r['app']})" + ("" if r["volume_deleted"] is None else
                                                            f"; volume {'deleted' if r['volume_deleted'] else 'will be deleted once its pod stops'}")))
    _run(go)


# ---------------------------------------------------------------- pipelines (DataOS Nilus)
def _access_hint(e: PipelineAccessError) -> PlatformError:
    ctx = load_context(state.context, state.tenant)
    return PlatformError(f"{e} Ask in the dashboard: https://{ctx.fqdn}/dapp/pipelines/new "
                         f"(it sends the request to the Depot's owner). Nothing was created.")


@pipeline_app.command("depots")
def pipeline_depots() -> None:
    """Depots in the tenant: what each can be moved from (batch, cdc, stream, metadata) and whether it can be written to."""
    def go():
        rows = _platform().depots()
        _out(rows, lambda rs: _table([{**r, "source": ",".join(r["modes"]) or "-", "destination": "yes" if r["destination"] else "-"}
                                      for r in rs], ["name", "label", "source", "destination", "owner"]))
    _run(go)


@pipeline_app.command("kinds")
def pipeline_kinds() -> None:
    """The answers to "how does the data change?" (--how) and the Nilus pipeline each one makes."""
    rows = [{"how": h.key, "means": h.title, "type": h.type, "strategy": h.strategy or "-",
             "schedule": "yes" if h.scheduled else ("continuous" if h.type in ("cdc", "stream") else "once"),
             "needs": ",".join(h.needs) or "-"} for h in HOWS.values()]
    _out(rows, lambda rs: _table(rs, ["how", "means", "type", "strategy", "schedule", "needs"]))


@pipeline_app.command("new")
def pipeline_new(
    name: str = typer.Argument(..., metavar="NAME"),
    source: str = typer.Option(..., "--from", help="DEPOT:OBJECT, e.g. pgrds:public.orders (just DEPOT for --how catalog)"),
    dest: str = typer.Option("", "--to", help="DEPOT or DEPOT:SCHEMA.TABLE (default table raw.<object>)"),
    how: str = typer.Option("once", "--how", help="once | refresh | new_rows | changes | live | live_history | events | catalog "
                                                   "(see `dapp pipeline kinds`)"),
    schedule: str = typer.Option("daily", "--schedule", help="hourly | every-6h | daily | weekly | a cron line (UTC)"),
    incremental_key: str = typer.Option("", "--incremental-key", help="column that tells new or changed rows apart"),
    primary_key: str = typer.Option("", "--primary-key", help="column that identifies a row"),
    mask: list[str] = typer.Option(None, "--mask", help="COLUMN=hash|redact|partial:N, repeatable"),
    skip: list[str] = typer.Option(None, "--skip", help="column to leave out, repeatable"),
    limit: int | None = typer.Option(None, "--limit", help="row limit, for a first try (batch)"),
    mode: str = typer.Option("shallow", "--mode", help="catalog depth: shallow | deep"),
    compute: str = typer.Option("engineering-compute", "--compute"),
    cpu: str = typer.Option("200m", "--cpu"), memory: str = typer.Option("512Mi", "--memory"),
    cpu_limit: str = typer.Option("1", "--cpu-limit"), memory_limit: str = typer.Option("2Gi", "--memory-limit"),
    description: str = typer.Option("", "--description", "-d"),
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be created; create nothing"),
) -> None:
    """Make a pipeline and start it. It runs as you; a Depot you may not use is reported, never granted."""
    def go():
        src_depot, _, obj = source.partition(":")
        dst_depot, _, dst_table = dest.partition(":")
        masks = {}
        for m in mask or []:
            col, _, kind = m.partition("=")
            masks[col.strip()] = kind.strip() or "hash"
        req = PipelineRequest(name=name, how=how, source_depot=src_depot, source_object=obj, dest_depot=dst_depot,
                              dest_table=dst_table, schedule=schedule, incremental_key=incremental_key, primary_key=primary_key,
                              mask=masks, exclude_columns=list(skip or []), row_limit=limit, metadata_mode=mode, compute=compute,
                              cpu=cpu, memory=memory, cpu_limit=cpu_limit, memory_limit=memory_limit, description=description)
        p = _platform()
        if dry_run:
            plan = p.plan_pipeline(req)
            _out({"describe": plan["describe"], "manifest": plan["manifest"]},
                 lambda r: (console.print(f"[bold]{r['describe']}[/bold]"), console.print(dump_yaml(r["manifest"]))))
            return
        try:
            done = p.deploy_pipeline(req)
        except PipelineAccessError as e:
            raise _access_hint(e) from e
        _out(done, lambda r: console.print(f"started {r['name']}: {r['describe']}\n"
                                           f"watch it: dapp pipeline status {r['name']}"))
    _run(go)


@pipeline_app.command("ls")
def pipeline_ls(mine: bool = typer.Option(False, "--mine", help="only yours")) -> None:
    """Pipelines in the tenant, with their latest run state."""
    def go():
        p = _platform()
        rows = p.pipelines(everyone=not mine)
        for r in rows:
            r["phase"] = p.pipeline_phase(r["name"])
            r["when"] = r["cron"] or ("continuous" if r["type"] in ("cdc", "stream") else "once")
        _out(rows, lambda rs: _table(rs, ["name", "type", "source", "object", "dest", "dest_table", "when", "phase", "owner"]))
    _run(go)


@pipeline_app.command("status")
def pipeline_status(name: str = typer.Argument(..., metavar="NAME"), runs: int = typer.Option(5, "--runs")) -> None:
    """One pipeline: what it does, its state, the last runs (rows, seconds) and 7-day stats."""
    def go():
        p = _platform()
        out = {**pipeline_summary(p.pipeline(name)), "phase": p.pipeline_phase(name),
               "runs": p.pipeline_runs(name, limit=runs), "stats_7d": p.pipeline_stats(name)}

        def show(r):
            console.print(f"[bold]{r['name']}[/bold] ({r['type']}) {r['phase']}: {r['description']}")
            _table(r["runs"], ["started", "ok", "rows", "seconds", "dest", "error"], title="runs")
        _out(out, show)
    _run(go)


@pipeline_app.command("logs")
def pipeline_logs(name: str = typer.Argument(..., metavar="NAME"), tail: int = typer.Option(120, "-n")) -> None:
    """The log of the latest run."""
    _run(lambda: _out(_platform().pipeline_logs(name, tail=tail)))


@pipeline_app.command("run")
def pipeline_run(name: str = typer.Argument(..., metavar="NAME")) -> None:
    """Run a batch or catalog pipeline again now (DataOS only runs it again when it is re-created; this does that)."""
    def go():
        try:
            done = _platform().rerun_pipeline(name)
        except PipelineAccessError as e:
            raise _access_hint(e) from e
        _out(done, lambda r: console.print(f"running {r['name']} again; `dapp pipeline status {r['name']}` in a minute"))
    _run(go)


@pipeline_app.command("rm")
def pipeline_rm(name: str = typer.Argument(..., metavar="NAME"),
                drop_table: bool = typer.Option(False, "--drop-table", help="also drop its destination table in the Lakehouse "
                                                "(only if this pipeline wrote it and no other pipeline does); can't be undone"),
                remove_source: bool = typer.Option(False, "--remove-source", help="also remove the source you connected for it "
                                                   "(Depot, Secret, grants), if no other pipeline uses it"),
                yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)")) -> None:
    """Delete a pipeline. By default the data it wrote stays; --drop-table and --remove-source take those too."""
    def go():
        p = _platform()
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to delete without --yes")
            left = p.pipeline_leftovers(name) if (drop_table or remove_source) else {}
            extra = (f" and drop table {left['table']['table']} ({left['table'].get('rows')} rows)"
                     if drop_table and left.get("table") else "") + (" and remove its source" if remove_source else "")
            typer.confirm(f"Delete pipeline {name}{extra}?", abort=True)
        out = p.delete_pipeline(name, drop_table=drop_table, remove_source=remove_source)
        _out(out, lambda r: console.print(" ".join([f"deleted pipeline {r['name']}."] + [x for x in (r["table_dropped"],) if x] +
                                                   ([f"removed source {r['source_removed']}."] if r["source_removed"] else []) +
                                                   r["problems"])))
        if out["problems"]:
            raise typer.Exit(1)
    _run(go)


@pipeline_app.command("leftovers")
def pipeline_leftovers(name: str = typer.Argument(..., metavar="NAME")) -> None:
    """What `dapp pipeline rm NAME` could also remove (its table, its source) and what must stay, and why."""
    _run(lambda: _out(_platform().pipeline_leftovers(name)))


# ---------------------------------------------------------------- sources (new Depots)
def _secret_inputs(secret_env: list[str] | None, secret_file: list[str] | None) -> dict[str, SecretStr]:
    out: dict[str, SecretStr] = {}
    for item in secret_env or []:
        key, _, var = item.partition("=")
        if not var:
            raise ValueError(f"--secret-env takes FIELD=ENV_VAR, e.g. password=PGPASSWORD (got {key!r})")
        if var not in os.environ:
            raise ValueError(f"environment variable {var} is not set (set it with `read -s {var}; export {var}`)")
        out[key.strip()] = SecretStr(os.environ[var])
    for item in secret_file or []:
        key, _, path = item.partition("=")
        if not path:
            raise ValueError(f"--secret-file takes FIELD=PATH (got {key!r})")
        out[key.strip()] = SecretStr(Path(path).expanduser().read_text())
    return out


@source_app.command("types")
def source_types(name: str = typer.Argument(None, metavar="[TYPE]")) -> None:
    """Depot types DataOS has here, and the fields each one asks for (from DataOS's own schema)."""
    def go():
        types = _platform().source_types()
        if name:
            t = types.get(name)
            if t is None:
                raise PlatformError(f"no Depot type {name}; see `dapp source types`")
            rows = [{"field": f.key, "where": "--set" if f.side == "spec" else "--secret-env", "required": f.required,
                     "kind": f.kind, "default": f.default} for f in t.fields]
            _out(rows, lambda rs: _table(rs, ["field", "where", "required", "kind", "default"], title=name))
            return
        rows = [{"type": k, "presets": ", ".join(p.key for p in PRESETS.values() if p.type == k)} for k in sorted(types)]
        _out(rows, lambda rs: _table(rs, ["type", "presets"]))
    _run(go)


@source_app.command("add")
def source_add(
    name: str = typer.Argument(..., metavar="NAME", help="the Depot name: lower-case letters and digits"),
    preset: str = typer.Option("", "--preset", help="supabase | neon | rds-postgres | postgresql | mysql | snowflake | mongodb"),
    type_: str = typer.Option("", "--type", help="a Depot type (see `dapp source types`); implied by --preset or --url-env"),
    url_env: str = typer.Option("", "--url-env", help="ENV_VAR holding a connection string (postgresql://user:pass@host:5432/db)"),
    sets: list[str] = typer.Option(None, "--set", help="connection field FIELD=VALUE, e.g. host=db.example.com (repeatable)"),
    secret_env: list[str] = typer.Option(None, "--secret-env", help="credential FIELD=ENV_VAR, e.g. password=PGPASSWORD"),
    secret_file: list[str] = typer.Option(None, "--secret-file", help="credential FIELD=PATH, e.g. gcp_json_key=key.json"),
    description: str = typer.Option("", "--description", "-d"),
) -> None:
    """Connect a new source: a Secret with its credentials and a Depot on top, both yours, and Can Use on them for you.
    Then `dapp pipeline new ... --from NAME:schema.table` brings its data in."""
    def go():
        spec: dict[str, Any] = {}
        creds = _secret_inputs(secret_env, secret_file)
        kind = type_ or (PRESETS[preset].type if preset in PRESETS else "")
        if preset and preset not in PRESETS:
            raise ValueError(f"unknown preset {preset}; one of {', '.join(PRESETS)}")
        if url_env:
            if url_env not in os.environ:
                raise ValueError(f"environment variable {url_env} is not set (set it with `read -s {url_env}; export {url_env}`)")
            kind_u, spec, from_creds = from_url(os.environ[url_env])
            kind = kind or kind_u
            creds = {**{k: SecretStr(v) for k, v in from_creds.items()}, **creds}
        for item in sets or []:
            k, _, v = item.partition("=")
            if k.strip() in ("password", "token", "key") or k.strip().endswith("_key"):
                raise ValueError(f"{k} is a credential: pass it with --secret-env {k}=ENV_VAR, not --set")
            if k.strip() == "params":
                spec["params"] = dict(p.split(":", 1) for p in v.split(",") if ":" in p)
            else:
                spec[k.strip()] = v
        if not kind:
            raise ValueError("say what it is: --preset supabase, --type postgresql, or --url-env with a connection string")
        req = SourceRequest(name=name, type=kind, spec=spec, secret=creds, description=description, preset=preset)
        done = _platform().add_source(req)
        _out(done, lambda r: console.print(
            f"connected {r['name']} ({r['type']}): Depot {r['name']}, credentials in Secret {r['secret']} (yours only).\n"
            f"bring data in: dapp pipeline new NAME --from {r['name']}:schema.table --to LAKEHOUSE_DEPOT --how once --dry-run"))
    _run(go)


@source_app.command("ls")
def source_ls() -> None:
    """The sources you connected."""
    _run(lambda: _out(_platform().sources(), lambda rs: _table(rs, ["name", "label", "where", "modes", "secret", "created"])))


@source_app.command("creds")
def source_creds(
    name: str = typer.Argument(..., metavar="NAME"),
    url_env: str = typer.Option("", "--url-env", help="ENV_VAR holding the new connection string"),
    secret_env: list[str] = typer.Option(None, "--secret-env", help="FIELD=ENV_VAR"),
    secret_file: list[str] = typer.Option(None, "--secret-file", help="FIELD=PATH"),
) -> None:
    """Replace a source's credentials (e.g. after a password change). Pipelines use them from their next run."""
    def go():
        creds = _secret_inputs(secret_env, secret_file)
        if url_env:
            if url_env not in os.environ:
                raise ValueError(f"environment variable {url_env} is not set")
            creds = {**{k: SecretStr(v) for k, v in from_url(os.environ[url_env])[2].items()}, **creds}
        _out(_platform().replace_source_credentials(name, creds),
             lambda r: console.print(f"replaced the credentials of {r['name']} (Secret {r['secret']})"))
    _run(go)


@source_app.command("rm")
def source_rm(name: str = typer.Argument(..., metavar="NAME"),
              keep_secret: bool = typer.Option(False, "--keep-secret", help="leave the Secret with the credentials"),
              yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)")) -> None:
    """Remove a source you connected: its Depot, its Secret (the credentials) and the access grants on it."""
    def go():
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to delete without --yes")
            typer.confirm(f"Remove source {name}? Pipelines that use it stop working.", abort=True)
        _out(_platform().delete_source(name, keep_secret=keep_secret), lambda r: console.print(
            f"removed {r['name']}" + ("; its Secret too" if r["secret_deleted"] else "") +
            (f"; these pipelines used it: {', '.join(r['pipelines_affected'])}" if r["pipelines_affected"] else "")))
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


# ---------------------------------------------------------------- tools
@tool_app.command("ls")
def tool_ls() -> None:
    """MCP tools you own or may use in this tenant."""
    def show(rows):
        _table([{**r, "mine": "yes" if r.get("mine") else "", "credential": "yes" if r["has_auth"] else ""}
                for r in rows], ["name", "owner", "mine", "credential", "description"])
    _run(lambda: _out(_platform().tools(), show))


@tool_app.command("add")
def tool_add(
    name: str = typer.Argument(..., help="Short name, e.g. jira (lower-case, digits, -)"),
    url: str = typer.Option(..., "--url", help="The MCP server's Streamable HTTP endpoint, e.g. https://host/mcp"),
    description: str = typer.Option("", "--description", "-d"),
    auth_header: str = typer.Option("", "--auth-header", help="Header the tool expects (default Authorization)"),
    auth_env: str | None = typer.Option(None, "--auth-env", metavar="VAR",
                                        help="Read the credential from this environment variable. A bare token "
                                             "is sent as `Authorization: Bearer <token>`"),
    auth_stdin: bool = typer.Option(False, "--auth-stdin", help="Read the credential value from stdin"),
    no_auth: bool = typer.Option(False, "--no-auth", help="Remove a stored credential"),
    allow: list[str] = typer.Option(None, "--allow", help='Who may use it: user id/email, "*", app:<name>'),
) -> None:
    """Register an MCP server with the gateway (or update yours). The credential is sent to the gateway once,
    stored encrypted there, and injected on every call: people and apps using the tool never see it."""
    def go():
        value: str | None = None
        if auth_env:
            value = os.environ.get(auth_env) or ""
            if not value:
                raise PlatformError(f"${auth_env} is empty or unset")
        elif auth_stdin:
            value = sys.stdin.read().strip()
            if not value:
                raise PlatformError("no credential on stdin")
        elif no_auth:
            value = ""
        r = _platform().save_tool(name, url=url, description=description, auth_header=auth_header,
                                  auth_value=value, allow=allow or None)

        def show(r):
            console.print(f"[green]tool {r['name']}[/green]  owner {r['owner']}  credential: "
                          f"{'stored (encrypted in the gateway)' if r['has_auth'] else 'none'}")
            console.print(f"allowed: {', '.join(r.get('allow') or []) or 'only you'}")
            console.print(f"MCP url: {r['mcp_url']}")
            console.print(f"use it in Claude Code:  dapp tool connect {r['name']}")
        _out(r, show)
    _run(go)


@tool_app.command("rm")
def tool_rm(name: str, yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask (agents must pass this)")) -> None:
    """Unregister a tool you own. Its MCP server keeps running; the gateway just stops serving it."""
    def go():
        if not yes:
            if state.json or not sys.stdin.isatty():
                raise PlatformError("refusing to remove without --yes")
            typer.confirm(f"Remove tool {name}? Everyone using it loses access.", abort=True)
        _out(_platform().delete_tool(name), lambda r: console.print(f"removed {r['tool']}"))
    _run(go)


def _show_tool_access(r):
    console.print(f"{r['tool']}  owner {r['owner']}\nallowed: {', '.join(r['allow']) or 'only the owner'}")


@tool_app.command("allow")
def tool_allow(name: str, who: list[str] = typer.Argument(..., help='user ids/emails, "*" (every DataOS user) '
                                                                    'or app:<name> (one of your apps)')) -> None:
    """Let people or apps use a tool."""
    _run(lambda: _out(_platform().tool_allow(name, who), _show_tool_access))


@tool_app.command("deny")
def tool_deny(name: str, who: list[str] = typer.Argument(...)) -> None:
    """Take a tool away from people or apps."""
    _run(lambda: _out(_platform().tool_deny(name, who), _show_tool_access))


@tool_app.command("usage")
def tool_usage(name: str | None = typer.Argument(None),
               hours: int = typer.Option(24 * 30, help="Window"),
               by: str = typer.Option("tool,caller", "--by",
                                      help="Group by: tool, caller, end_user, method, tool_name, day")) -> None:
    """Calls to your tools: who called, which function, how many failed. `--by caller,tool_name`."""
    def go():
        r = _platform().tool_usage(name, since_hours=hours, group_by=by)

        def show(r):
            cols = [c for c in by.split(",") if c] + ["calls", "errors", "avg_latency_ms"]
            _table(r["rows"], cols)
            console.print(f"total: {r['total']['calls']} calls, {r['total']['errors']} errors")
        _out(r, show)
    _run(go)


@tool_app.command("connect")
def tool_connect(
    name: str,
    as_name: str | None = typer.Option(None, "--as", help="Name in Claude Code (default: the tool name)"),
    scope: str = typer.Option("user", help="Claude Code scope: user, project or local"),
    print_only: bool = typer.Option(False, "--print", help="Only print the command"),
) -> None:
    """Add a tool to Claude Code. It runs `dapp tool bridge`, which signs each call with your current DataOS login."""
    import shutil
    import subprocess

    def go():
        p = _platform()
        p.tool(name)  # fails early if it doesn't exist or you may not use it
        dapp_bin = shutil.which("dapp") or sys.argv[0]
        cmd = ["claude", "mcp", "add", "--scope", scope, as_name or name, "--", dapp_bin]
        if state.context:
            cmd += ["--context", state.context]
        cmd += ["tool", "bridge", name]
        if print_only or not shutil.which("claude"):
            _out({"command": cmd}, lambda r: console.print(" ".join(r["command"])))
            return
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise PlatformError(f"claude mcp add failed: {(res.stderr or res.stdout).strip()}")
        _out({"added": as_name or name, "command": cmd},
             lambda r: console.print(f"added '{r['added']}' to Claude Code; restart Claude Code to use it"))
    _run(go)


@tool_app.command("bridge", hidden=True)
def tool_bridge(name: str) -> None:
    """stdio MCP server that forwards to the gateway (what `dapp tool connect` registers)."""
    from .bridge import run as bridge

    ctx = load_context(state.context, state.tenant)
    url = Platform(DataOS(ctx)).tool_url(name)
    bridge(url, lambda: load_context(state.context, state.tenant).auth_headers())


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
