"""A personal Claude Code, deployed from the dashboard: one per person, private, with optional stored Claude token."""

import base64

import pytest
from test_platform import make

from dapp.claude import (
    CLAUDE_CODE_IMAGE,
    DEFAULTS,
    TOKEN_KEY,
    ClaudeDeploy,
    claude_app_spec,
    clean_token,
    instance_name,
    token_secret,
)
from dapp.platform import PlatformError
from dapp.spec import app_tag

TOKEN_LINE_1 = "sk-ant-oat01-" + "A1b2C3d4" * 7
TOKEN_LINE_2 = "Zz9-_Yy8"


def test_each_person_gets_their_own_valid_app_name():
    assert instance_name("akshaychametmdcio") == "claude-akshaychametmdcio"
    assert instance_name("Alice.Smith@corp.io") == "claude-alice-smith-corp-io"
    long = instance_name("x" * 90)
    assert long.startswith("claude-") and len(long) <= 48 and not long.endswith("-")
    assert instance_name("") == "claude-me" and token_secret("claude-alice") == "dapp-claude-alice-anthropic"


def test_a_pasted_token_is_cleaned_and_checked():
    assert clean_token(f"  {TOKEN_LINE_1}\n{TOKEN_LINE_2}\n") == TOKEN_LINE_1 + TOKEN_LINE_2     # a wrapped token comes in two lines
    assert clean_token("") == "" and clean_token("   \n") == ""
    for bad in ("short", "x" * 60 + "<script>", "tok en " + "y" * 60 + "!"):
        with pytest.raises(ValueError, match="Claude token"):
            clean_token(bad)

@pytest.mark.parametrize("change, message", [
    ({"memory": "256Mi", "memory_limit": "512Mi"}, "at least 512Mi"), ({"memory_limit": "32Gi"}, "16Gi"), ({"cpu": "4", "cpu_limit": "2"}, "cpu"),
    ({"cpu_limit": "12"}, "8 cores"), ({"cpu": "lots"}, "cpu"), ({"compute": "Bad Name"}, "compute")])
def test_resources_are_checked_with_a_sentence(change, message):
    with pytest.raises(ValueError, match=message):
        ClaudeDeploy(**change).check()


def test_the_defaults_are_cheap_and_valid():
    d = ClaudeDeploy().check()
    assert (d.cpu, d.cpu_limit, d.memory, d.memory_limit) == (DEFAULTS["cpu"], DEFAULTS["cpu_limit"], DEFAULTS["memory"], DEFAULTS["memory_limit"])


def test_the_app_is_a_private_terminal_for_its_owner():
    spec = claude_app_spec("claude-alice", "alice", ClaudeDeploy().check(), compute="engineering-compute", fqdn="x.dataos.cloud", with_token=True)
    assert spec.image == CLAUDE_CODE_IMAGE and spec.command == ["/opt/claude/start.sh"] and spec.path == "/claude-alice"
    assert spec.public is False and spec.uses_login and spec.secrets == ["dapp-claude-alice-anthropic"] and spec.metrics
    assert spec.env == {"ROOT_PATH": "/claude-alice", "DATAOS_FQDN": "x.dataos.cloud", "DAPP_DEFAULT_COMPUTE": "engineering-compute"}
    assert "volumes" not in spec.model_fields_set and not spec.volumes and spec.disk is None            # its files live in immortal-obj
    assert claude_app_spec("claude-alice", "alice", ClaudeDeploy(), compute="c", fqdn="x", with_token=False).secrets == []


def test_deploy_stores_the_token_as_a_secret_and_deploys_the_app():
    p, fake = make()
    out = p.deploy_claude(ClaudeDeploy(), compute="engineering-compute", token=f"{TOKEN_LINE_1}\n{TOKEN_LINE_2}")
    assert out["app"] == "claude-alice" and out["url"].endswith("/claude-alice/") and out["token"] is True
    app = fake.store[("app", "claude-alice")]["spec"]["service"]
    assert app["image"] == CLAUDE_CODE_IMAGE and {"id": "dapp-claude-alice-anthropic"} in app["secrets"]
    secret = fake.store[("secret", "dapp-claude-alice-anthropic")]
    assert base64.b64decode(secret["spec"]["data"][TOKEN_KEY]).decode() == TOKEN_LINE_1 + TOKEN_LINE_2
    assert app_tag("claude-alice") in secret["tags"]                                                    # tagged with the app, so rm --purge finds it
    with pytest.raises(PlatformError, match="already deployed"):
        p.deploy_claude(ClaudeDeploy(), compute="engineering-compute")
    inst = p.claude_instance()
    assert inst["exists"] and inst["token"] and inst["app"] == "claude-alice" and inst["state"] is not None


def test_without_a_token_nothing_is_attached_and_it_can_be_added_later():
    p, fake = make()
    p.deploy_claude(ClaudeDeploy(), compute="engineering-compute")
    assert ("secret", "dapp-claude-alice-anthropic") not in fake.store and p.claude_instance()["token"] is False
    assert not [s for s in fake.store[("app", "claude-alice")]["spec"]["service"].get("secrets", []) if "anthropic" in s["id"]]
    done = p.set_claude_token(TOKEN_LINE_1 + TOKEN_LINE_2)                                              # running: attached, it restarts
    assert done["attached"] is True and p.claude_instance()["token"] is True
    assert {"id": "dapp-claude-alice-anthropic"} in fake.store[("app", "claude-alice")]["spec"]["service"]["secrets"]
    with pytest.raises(PlatformError, match="paste the token"):
        p.set_claude_token("")
    with pytest.raises(ValueError, match="Claude token"):
        p.set_claude_token("nope")


def test_stop_keeps_the_token_and_deploying_again_uses_it():
    p, fake = make()
    p.deploy_claude(ClaudeDeploy(), compute="engineering-compute", token=TOKEN_LINE_1 + TOKEN_LINE_2)
    p.stop_claude()
    assert ("app", "claude-alice") not in fake.store and ("secret", "dapp-claude-alice-anthropic") in fake.store    # only the pod is gone
    assert p.claude_instance() | {"state": None} == {"app": "claude-alice", "exists": False, "state": None, "token": True,
                                                    "url": "https://x.dataos.cloud/claude-alice/"}
    again = p.deploy_claude(ClaudeDeploy(), compute="engineering-compute")                               # the stored token is used
    assert again["token"] is True and {"id": "dapp-claude-alice-anthropic"} in fake.store[("app", "claude-alice")]["spec"]["service"]["secrets"]


def test_delete_removes_the_app_and_its_secrets(monkeypatch):
    p, _ = make()
    calls = []
    monkeypatch.setattr(p, "delete", lambda app, purge=False: calls.append((app, purge)) or {"output": "ok"})
    p.delete_claude()
    assert calls == [("claude-alice", True)]


def test_everyone_sees_who_has_one():
    p, fake = make()
    p.deploy_claude(ClaudeDeploy(), compute="engineering-compute")
    fake.store[("app", "claude-bob")] = {"name": "claude-bob", "type": "app", "owner": "bob", "tenant": "ai", "tags": [], "spec": {}}
    fake.store[("app", "chatbot")] = {"name": "chatbot", "type": "app", "owner": "bob", "tenant": "ai", "tags": [], "spec": {}}
    assert sorted(a["name"] for a in p.claude_instances()) == ["claude-alice", "claude-bob"]


def test_deleting_a_brand_new_app_waits_for_dataos_to_grant_the_right(monkeypatch):
    """A delete right after a deploy can get a 403 that clears by itself (DataOS adds the creator's rights a little later)."""
    from datetime import UTC, datetime, timedelta

    from dapp.client import DataOSError

    class Denying:
        """The real writer, except that deleting an app is refused the first `refusals` times."""

        def __init__(self, inner, refusals):
            self.inner, self.refusals, self.name = inner, refusals, inner.name

        def delete(self, kind, name, **kw):
            if kind == "app" and self.refusals > 0:
                self.refusals -= 1
                raise DataOSError("delete failed: HTTP 403 (no permission)", 403)
            return self.inner.delete(kind, name, **kw)

    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)

    p, fake = make()
    p.deploy_claude(ClaudeDeploy(), compute="engineering-compute")
    fake.store[("app", "claude-alice")]["createdAt"] = datetime.now(UTC).isoformat()
    p.writer = Denying(p.writer, refusals=2)
    p.stop_claude()
    assert sleeps == [5, 5] and ("app", "claude-alice") not in fake.store                      # waited twice, then it worked

    p2, fake2 = make()
    p2.deploy_claude(ClaudeDeploy(), compute="engineering-compute")
    fake2.store[("app", "claude-alice")]["createdAt"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()   # an old app
    p2.writer = Denying(p2.writer, refusals=99)
    sleeps.clear()
    with pytest.raises(DataOSError, match="403"):
        p2.stop_claude()                                                                       # a real missing permission: no waiting
    assert sleeps == []
