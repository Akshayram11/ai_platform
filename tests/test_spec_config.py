import pytest
import yaml

from dapp.config import ConfigError, load_context
from dapp.spec import AppSpec, render_app, render_grant, wire_manifest


def spec(**kw):
    base = {"name": "chatbot", "description": "d", "image": "img:1", "compute": "c1", "command": ["run"]}
    return AppSpec.model_validate({**base, **kw})


def test_render_app_matches_app_domain_shape():
    m = render_app(spec(env={"A": "1"}, secrets=["s1"], llm=True, access={"users": ["bob"]}),
                   tenant="ai", user_id="alice", fqdn="x.dataos.cloud")
    assert m["type"] == "app" and m["version"] == "v1alpha" and m["tenant"] == "ai"
    s = m["spec"]
    assert s["compute"] == "c1"
    assert s["app"]["owners"] == ["alice"] and s["app"]["domain"] == "ai"
    svc = s["service"]
    assert svc["http"]["path"] == "/chatbot" and svc["http"]["noNetworkAuthorization"] is True
    assert svc["http"]["oidcClient"] == {"enabled": True, "clientId": "dapp-ai-chatbot",
                                         "clientSecret": "ai:dapp-chatbot-oidc",
                                         "redirectPaths": ["/chatbot/_dapp/callback"]}
    assert svc["envs"]["DAPP_ALLOWED_USERS"] == "alice,bob" and svc["envs"]["A"] == "1"
    assert svc["envs"]["DAPP_PUBLIC_URL"] == "https://x.dataos.cloud/chatbot"
    assert {"id": "dapp-chatbot-oidc"} in svc["secrets"]
    assert s["runAsUser"] == "alice"
    assert {"id": "s1"} in svc["secrets"] and {"id": "dapp-chatbot-llm"} in svc["secrets"]
    assert "dapp" in m["tags"] and "dapp-app-chatbot" in m["tags"]


def test_public_app_has_no_login():
    svc = render_app(spec(public=True), tenant="ai", user_id="alice")["spec"]["service"]
    assert svc["http"]["noNetworkAuthorization"] is True and "oidcClient" not in svc["http"]
    assert "DAPP_AUTH" not in svc.get("envs", {})


def test_bad_name_and_missing_compute():
    with pytest.raises(ValueError):
        spec(name="Bad_Name")
    with pytest.raises(ValueError):
        render_app(spec(compute=None, public=True), tenant="ai", user_id="alice")


def test_grant_shape():
    g = render_grant(name="g", tenant="ai", use_case="uc", subjects=["users:id:bob"], values={},
                     requester="alice", notes="n", app="chatbot")
    assert g["grant"]["collection"] == "ai" and g["grant"]["policy_use_case_id"] == "uc"


def test_load_context_from_ctl_files(tmp_path, monkeypatch):
    for v in ("DATAOS_FQDN", "DATAOS_TENANT_ID", "APIKEY", "USER_ID", "DATAOS_RUN_AS_USER",
              "DATAOS_RUN_AS_APIKEY", "DATAOS_ACCESS_TOKEN", "CONTEXT", "TENANT"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "dataos.context.yaml").write_text(yaml.safe_dump(
        {"contexts": {"current_context_name": "ai", "context_list": [{"name": "ai"}]}}))
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai" / ".dataos.tenancy.config.yaml").write_text(yaml.safe_dump(
        {"tenancy": {"dataos_fqdn": "x.dataos.cloud", "dataos_tenant": "ai"}}))
    (tmp_path / ".x.dataos.cloud.dataos.cred.config.yaml").write_text(yaml.safe_dump(
        {"user": {"id": "alice", "token": {"apikey": "", "access_token": "a.b.c"}}}))
    ctx = load_context(cfg=tmp_path)
    assert (ctx.fqdn, ctx.tenant, ctx.user_id) == ("x.dataos.cloud", "ai", "alice")
    assert ctx.auth_headers() == {"Authorization": "Bearer a.b.c"}
    with pytest.raises(ConfigError):
        load_context("missing", cfg=tmp_path)


def test_reserved_env_rejected():
    with pytest.raises(ValueError, match="set by DataOS"):
        spec(env={"DATAOS_TENANT_ID": "x"})


def test_env_values_survive_the_unquoted_yaml_template():
    import yaml

    from dapp.spec import yaml_plain
    for raw in ["*,alice", "true", "123", "a: b", "#x", "- y", "{a}", "https://x/y", "plain", "", "a\nb"]:
        rendered = f"k: {yaml_plain(raw)}"
        assert yaml.safe_load(rendered) == {"k": raw}, raw
    m = wire_manifest(render_app(spec(access={"users": ["*"]}), tenant="ai", user_id="alice",
                                 fqdn="x.dataos.cloud"))
    assert yaml.safe_load(f"k: {m['spec']['service']['envs']['DAPP_ALLOWED_USERS']}")["k"] == "*,alice"


def test_quoting_round_trips_and_never_stacks():
    from dapp.platform import live_envs, split_ids
    from dapp.spec import yaml_decode

    tricky = ["*,alice", "", "true", "a: b", "plain", "1"]
    for raw in tricky:
        wired = wire_manifest({"type": "app", "spec": {"service": {"envs": {"K": raw}}}})
        stored = wired["spec"]["service"]["envs"]["K"]
        assert yaml_decode(stored) == raw
        # re-reading a live app and writing it back again must not change anything
        again = wire_manifest({"type": "app", "spec": {"service": {"envs": live_envs(
            {"spec": {"service": {"envs": {"K": stored}}}})}}})
        assert again["spec"]["service"]["envs"]["K"] == stored
    assert split_ids('"\\"*,x\\"",*,alice,') == ["*", "alice"]  # junk from the old bug is dropped


def test_disk_makes_the_app_stateful_and_is_validated():
    base = {"public": True}
    svc = render_app(spec(**base, disk={"size": "1Gi", "mountPath": "/data"}), tenant="ai", user_id="a")["spec"]["service"]
    assert svc["stateful"] is True and svc["disk"] == {"size": "1Gi", "type": "disk", "mountPath": "/data"}
    assert "stateful" not in render_app(spec(**base), tenant="ai", user_id="a")["spec"]["service"]
    with pytest.raises(ValueError, match="single replica"):
        render_app(spec(**base, replicas=2, disk={"size": "1Gi", "mountPath": "/data"}), tenant="ai", user_id="a")
    with pytest.raises(ValueError, match="both mount"):
        render_app(spec(**base, disk={"size": "1Gi", "mountPath": "/data"},
                        workingDirs=[{"id": "d", "directory": "/data"}]), tenant="ai", user_id="a")
    with pytest.raises(ValueError):
        spec(**base, disk={"size": "lots", "mountPath": "/data"})
