import base64
import json

import httpx
import pytest
from pydantic import SecretStr
from test_platform_pipelines import Fake

from dapp.client import DataOS
from dapp.config import Context
from dapp.ctl import ApiWriter
from dapp.platform import Platform, PlatformError
from dapp.sources import SourceRequest, from_url

PW = "s3cr3t/p@ss:w0rd"
URL = "postgresql://postgres.abcd:s3cr3t%2Fp%40ss%3Aw0rd@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
TYPES = [{"name": "postgresql",
          "specSchema": json.dumps({"required": ["subprotocol", "host", "port", "database"],
                                    "properties": {"host": {"type": "string"}, "port": {"type": "integer"},
                                                   "params": {"type": "object"}, "database": {"type": "string"},
                                                   "subprotocol": {"type": "string"}}}),
          "secretSchema": json.dumps({"required": ["username", "password"],
                                      "properties": {"password": {"type": "string"}, "username": {"type": "string"}}})}]


class SourceFake(Fake):
    """DataOS refuses a Depot until its creator holds Can Use on the Depot's Secret (as the docs say)."""

    def __init__(self, me, *, fail_depot=None, **kw):
        super().__init__(me, **kw)
        self.fail_depot, self.sent = fail_depot, []

    def __call__(self, req):
        if req.url.path == "/ds/api/v2/types":
            return httpx.Response(200, json=TYPES)
        if req.method in ("POST", "PUT"):
            self.sent.append(req.content.decode())
            body = json.loads(req.content)
            if body["type"] == "depot":
                if self.fail_depot:
                    return httpx.Response(400, json={"message": self.fail_depot})
                sid = body["spec"]["secrets"][0]["id"]
                ok = any(g.get("spec", {}).get("policy_use_case_id") == "use-specific-secret"
                         and {"id": sid} in g["spec"]["values"]["read-specific-secret-governance"]
                         and g["spec"]["subjects"] == [f"users:id:{self.me}"]
                         for (t, _), g in self.store.items() if t == "grant")
                if not ok:
                    return httpx.Response(400, json={"message": f"not-allowed, object 'dataos:type:secret:{sid}'"})
        return super().__call__(req)


def make(**kw):
    fake = SourceFake("alice", **kw)
    client = DataOS(Context("t", "x.dataos.cloud", "ai", "alice", "key", None), transport=httpx.MockTransport(fake))
    return Platform(client, writer=ApiWriter(client)), fake


def supabase(name="supabase"):
    kind, spec, creds = from_url(URL)
    return SourceRequest(name=name, type=kind, spec=spec, secret={k: SecretStr(v) for k, v in creds.items()}, preset="supabase")


def test_connect_a_source_in_the_right_order_and_only_for_yourself():
    p, fake = make()
    done = p.add_source(supabase())
    order = [json.loads(b)["type"] for b in fake.sent]
    assert order == ["secret", "grant", "depot", "grant"]                    # Secret, its Can Use, Depot, its Can Use
    sec = fake.store[("secret", "dapp-src-supabase")]
    assert {k: base64.b64decode(v).decode() for k, v in sec["spec"]["data"].items()} == {"username": "postgres.abcd",
                                                                                         "password": PW}
    depot = fake.store[("depot", "supabase")]
    assert depot["owner"] == "alice" and depot["spec"]["spec"]["params"] == {"sslmode": "require"}
    grants = [g for (t, _), g in fake.store.items() if t == "grant"]
    assert {g["spec"]["policy_use_case_id"] for g in grants} == {"use-specific-secret", "use-specific-depot"}
    assert all(g["spec"]["subjects"] == ["users:id:alice"] for g in grants)   # the creator only, nobody else
    assert PW not in json.dumps(done) and PW not in json.dumps(depot)
    assert [s["name"] for s in p.sources()] == ["supabase"] and p.sources()[0]["secret"] == "ai:dapp-src-supabase"
    with pytest.raises(PlatformError, match="already exists"):
        p.add_source(supabase())


def test_a_failed_connect_cleans_up_and_hides_the_password():
    p, fake = make(fail_depot=f"bad host for postgresql://postgres.abcd:{PW}@h/db")
    with pytest.raises(PlatformError) as e:
        p.add_source(supabase())
    assert PW not in str(e.value) and "Could not connect supabase" in str(e.value)
    assert not [k for k in fake.store if k[0] in ("secret", "grant") or k == ("depot", "supabase")]


def test_replace_and_delete_only_your_own_source():
    p, fake = make()
    p.add_source(supabase())
    with pytest.raises(PlatformError, match="give all the credentials again"):
        p.replace_source_credentials("supabase", {"password": SecretStr("new")})
    p.replace_source_credentials("supabase", {"username": SecretStr("u2"), "password": SecretStr("new-pw")})
    data = fake.store[("secret", "dapp-src-supabase")]["spec"]["data"]
    assert base64.b64decode(data["password"]).decode() == "new-pw"
    with pytest.raises(PlatformError, match="not a source you connected"):
        p.delete_source("pgrds")                                             # someone else's Depot
    out = p.delete_source("supabase")
    assert out["secret_deleted"] and len(out["grants_removed"]) == 2 and ("depot", "supabase") not in fake.store and ("secret", "dapp-src-supabase") not in fake.store
