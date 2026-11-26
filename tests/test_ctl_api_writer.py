"""The API writer (used where dataos-ctl isn't installed: the Claude Code pod, the dashboard) must do what dataos-ctl does."""

import base64
from types import SimpleNamespace

from dapp.ctl import ApiWriter
from dapp.spec import render_secret


class FakeClient:
    def __init__(self, user_id="alice"):
        self.ctx = SimpleNamespace(user_id=user_id)
        self.applied = []

    def apply(self, manifest):
        self.applied.append(manifest)
        return {}


def test_owner_is_filled_in_and_secret_values_are_base64():
    c = FakeClient()
    secret = render_secret("dapp-x-oidc", tenant="engineering", data={"clientSecret": "tok_en-1", "DAPP_CODE_0": "QUJD"},
                           description="d", app="x")
    ApiWriter(c).apply(secret)
    sent = c.applied[0]
    assert sent["owner"] == "alice"                                      # "owner is required" otherwise
    data = sent["spec"]["data"]
    assert base64.b64decode(data["clientSecret"]) == b"tok_en-1"          # plain text is not valid base64Value
    assert base64.b64decode(data["DAPP_CODE_0"]) == b"QUJD"               # our own base64 text is encoded once more, as ctl does
    assert secret["spec"]["data"]["clientSecret"] == "tok_en-1"           # the caller's manifest is not changed


def test_other_resources_keep_their_body_and_an_explicit_owner_wins():
    c = FakeClient()
    app = {"name": "a", "type": "app", "version": "v1alpha", "tenant": "t", "spec": {"service": {"port": 8080}}}
    ApiWriter(c).apply(app, {**app, "name": "b", "owner": "bob"})
    assert c.applied[0]["spec"] == app["spec"] and c.applied[0]["owner"] == "alice"
    assert c.applied[1]["owner"] == "bob"
    anonymous = FakeClient(user_id=None)
    ApiWriter(anonymous).apply(app)
    assert "owner" not in anonymous.applied[0]                              # nothing to fill in: leave it to the server
