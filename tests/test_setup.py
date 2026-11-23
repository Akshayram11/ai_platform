import json
import os
import stat

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from dapp import config
from dapp.auth import Identity
from dapp.cli import app as cli
from dapp.doctor import run_checks
from dapp.spec import load_spec, render_app

runner = CliRunner()
ENV_VARS = ("DATAOS_FQDN", "DATAOS_TENANT_ID", "APIKEY", "USER_ID", "DATAOS_RUN_AS_USER", "DATAOS_RUN_AS_APIKEY",
            "DATAOS_ACCESS_TOKEN", "CONTEXT", "TENANT", "DAPP_SETUP_APIKEY", "DAPP_FQDN", "DAPP_TENANT")


@pytest.fixture()
def clean(monkeypatch, tmp_path):
    for v in ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DAPP_CONFIG", str(tmp_path / "dapp" / "config.yaml"))
    monkeypatch.setenv("DATAOS_CONFIG_DIRECTORY", str(tmp_path / "ctl"))   # no dataos-ctl on this "laptop"
    return tmp_path


class FakeHeimdall:
    def __init__(self, base_url, **kw):
        self.base = base_url

    def identify(self, token):
        return Identity("alice") if token == "good-key" else None


def test_saved_config_is_private_and_used_when_dataos_ctl_is_absent(clean):
    path = config.save_dapp_config(fqdn="x.dataos.cloud", tenant="t", user="alice", apikey="good-key")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    ctx = config.load_context()
    assert (ctx.name, ctx.fqdn, ctx.tenant, ctx.user_id) == ("dapp", "x.dataos.cloud", "t", "alice")
    assert ctx.auth_headers() == {"apikey": "good-key"} and "dapp" in config.list_contexts()


def test_a_working_dataos_ctl_login_wins_unless_dapp_is_asked_for(clean):
    config.save_dapp_config(fqdn="saved.cloud", tenant="t", user="alice", apikey="good-key")
    ctl = clean / "ctl"
    (ctl / "ai").mkdir(parents=True)
    (ctl / "dataos.context.yaml").write_text(yaml.safe_dump({"contexts": {"current_context_name": "ai",
                                                                         "context_list": [{"name": "ai"}]}}))
    (ctl / "ai" / ".dataos.tenancy.config.yaml").write_text(yaml.safe_dump(
        {"tenancy": {"dataos_fqdn": "ctl.cloud", "dataos_tenant": "ai"}}))
    (ctl / ".ctl.cloud.dataos.cred.config.yaml").write_text(yaml.safe_dump({"user": {"id": "bob", "token": {"apikey": "k2"}}}))
    assert config.load_context().fqdn == "ctl.cloud"                 # existing dataos-ctl users are unaffected
    assert config.load_context("dapp").fqdn == "saved.cloud"          # --context dapp opts in
    os.environ["DATAOS_FQDN"] = "env.cloud"; os.environ["DATAOS_TENANT_ID"] = "e"; os.environ["APIKEY"] = "ek"; os.environ["USER_ID"] = "u"
    try:
        assert config.load_context().fqdn == "env.cloud"              # environment variables beat everything
    finally:
        for v in ("DATAOS_FQDN", "DATAOS_TENANT_ID", "APIKEY", "USER_ID"):
            os.environ.pop(v)


def test_setup_checks_the_key_before_saving_it(clean, monkeypatch):
    monkeypatch.setattr("dapp.auth.Heimdall", FakeHeimdall)
    ok = runner.invoke(cli, ["--json", "setup", "--fqdn", "https://x.dataos.cloud/", "--tenant", "t"],
                       env={"DAPP_SETUP_APIKEY": "good-key"})
    assert ok.exit_code == 0, ok.output
    assert json.loads(ok.stdout)["user"] == "alice" and "good-key" not in ok.output        # the key is never echoed
    saved = yaml.safe_load(config.dapp_config_path().read_text())
    assert saved == {"fqdn": "x.dataos.cloud", "tenant": "t", "user": "alice", "apikey": "good-key"}

    config.dapp_config_path().unlink()
    bad = runner.invoke(cli, ["--json", "setup", "--fqdn", "x.dataos.cloud"], env={"DAPP_SETUP_APIKEY": "wrong"})
    assert bad.exit_code == 1 and "did not accept that API key" in bad.output and not config.dapp_config_path().exists()
    none = runner.invoke(cli, ["--json", "setup", "--fqdn", "x.dataos.cloud"])
    assert none.exit_code == 1 and "no API key" in none.output
    piped = runner.invoke(cli, ["--json", "setup", "--fqdn", "x.dataos.cloud", "--api-key-stdin"], input="good-key\n")
    assert piped.exit_code == 0 and config.load_dapp_config()["user"] == "alice"


def _doctor_transport(identity_ok=True, apps_status=200, gateway=True):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/heimdall/api/v1/authorize"):
            return httpx.Response(200, json={"allow": identity_ok, "valid": identity_ok,
                                             "result": {"id": "alice", "tags": []} if identity_ok else {}})
        if p.endswith("/resources/app"):
            return httpx.Response(apps_status, json={"resources": [{"name": "a"}]} if apps_status == 200 else {"m": "no"})
        if p.endswith("/resources/compute"):
            return httpx.Response(200, json={"resources": [{"name": "engineering-compute"}]})
        if p.endswith("/healthz"):
            return httpx.Response(200 if gateway else 502, json={"ok": True, "providers": {"openai": True}})
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def test_doctor_reports_each_requirement_and_stops_at_the_first_broken_one(clean, monkeypatch):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud"); monkeypatch.setenv("DATAOS_TENANT_ID", "t")
    monkeypatch.setenv("DATAOS_RUN_AS_USER", "alice"); monkeypatch.setenv("DATAOS_RUN_AS_APIKEY", "good-key")
    rows = {c["check"]: c for c in run_checks(transport=_doctor_transport())}
    assert rows["credentials"]["ok"] and "alice" in rows["credentials"]["detail"]
    assert rows["deploy access"]["ok"] and rows["compute"]["detail"] == "engineering-compute"
    assert rows["LLM gateway"]["ok"] and "openai" in rows["LLM gateway"]["detail"]
    assert not rows["dataos-ctl"]["required"] and not rows["docker"]["required"]   # reported, never blocking

    bad = run_checks(transport=_doctor_transport(identity_ok=False))
    assert bad[-1]["check"] == "credentials" and not bad[-1]["ok"] and "Keys page" in bad[-1]["hint"]
    assert "deploy access" not in {c["check"] for c in bad}                 # no point probing further with a bad key
    denied = {c["check"]: c for c in run_checks(transport=_doctor_transport(apps_status=403))}
    assert not denied["deploy access"]["ok"] and "grants" in denied["deploy access"]["hint"]


def test_doctor_exit_code_and_missing_setup(clean, monkeypatch):
    r = runner.invoke(cli, ["--json", "doctor"])
    out = json.loads(r.stdout)
    assert r.exit_code == 1 and out["ok"] is False and out["checks"][-1]["check"] == "context"
    assert "dataos-ctl login" in out["checks"][-1]["hint"]


def test_chatbot_template_is_a_valid_consistent_app(clean, tmp_path):
    f = tmp_path / "dapp.yaml"
    r = runner.invoke(cli, ["init", "mychat", "--template", "chatbot", "-f", str(f)])
    assert r.exit_code == 0, r.output
    spec = load_spec(f)
    m = render_app(spec, tenant="engineering", user_id="manager", fqdn="x.dataos.cloud")
    svc = m["spec"]["service"]
    assert m["spec"]["compute"] == "engineering-compute" and spec.llm and spec.metrics and not spec.public
    assert svc["http"]["path"] == "/mychat" and svc["envs"]["ROOT_PATH"] == "/mychat"           # path and ROOT_PATH agree
    assert svc["http"]["oidcClient"]["redirectPaths"] == ["/mychat/_dapp/callback"]
    assert {"id": "dapp-mychat-llm"} in svc["secrets"] and {"id": "dapp-mychat-oidc"} in svc["secrets"]
    assert runner.invoke(cli, ["init", "x", "--template", "nope", "-f", str(tmp_path / "y.yaml")]).exit_code == 1


def _jwt(exp_in):
    import base64
    import time

    body = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + exp_in}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def test_an_expired_login_says_so_instead_of_a_bare_401():
    live = config.Context("c", "x", "t", "alice", None, _jwt(3600))
    assert live.auth_headers()["Authorization"].startswith("Bearer ")
    with pytest.raises(config.ConfigError, match=r"login expired .* `dataos-ctl login`"):
        config.Context("c", "x", "t", "alice", None, _jwt(-1800)).auth_headers()
    assert config.token_seconds_left("not-a-jwt") is None
    assert config.Context("c", "x", "t", "alice", "key", _jwt(-1800)).auth_headers() == {"apikey": "key"}   # API keys don't expire here


def test_doctor_tells_the_user_to_run_dataos_ctl_login(clean, monkeypatch):
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud"); monkeypatch.setenv("DATAOS_TENANT_ID", "t")
    monkeypatch.setenv("DATAOS_ACCESS_TOKEN", _jwt(-600))
    last = run_checks(transport=_doctor_transport())[-1]
    assert last["check"] == "credentials" and not last["ok"] and "dataos-ctl login" in last["hint"]
