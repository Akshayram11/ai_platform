"""Connect a source from the dashboard: credentials go to DataOS and never come back to a page."""
import json
import re

import httpx
from test_dashboard import signed_in_with_fakes
from test_platform_sources import PW, URL, SourceFake

from dapp.client import DataOS
from dapp.config import Context
from dapp.ctl import ApiWriter
from dapp.platform import Platform


def test_connect_supabase_from_the_dashboard(monkeypatch):
    fake = SourceFake("alice")

    def platform(s, t=None):
        client = DataOS(Context("t", "x.dataos.cloud", "ai", "alice", "key", None), transport=httpx.MockTransport(fake))
        return Platform(client, writer=ApiWriter(client))

    c = signed_in_with_fakes(monkeypatch, platform_factory=platform)
    page = c.get("/sources")
    assert page.status_code == 200 and "Supabase" in page.text and "None yet" in page.text
    new = c.get("/sources/new", params={"preset": "supabase"})
    assert new.status_code == 200 and '"postgresql"' in new.text and "Session pooler" in new.text
    assert 'value="supabase"' in new.text and 'aria-current="page"' in new.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', new.text).group(1)

    bad = c.post("/sources/new", data={"csrf": csrf, "preset": "supabase", "name": "Supa-Base", "url": URL})
    assert bad.status_code == 400 and "lower-case letters and digits" in bad.text
    assert PW not in bad.text and "s3cr3t" not in bad.text and "emptied" in bad.text     # never echoed back

    ok = c.post("/sources/new", data={"csrf": csrf, "preset": "supabase", "name": "supabase", "url": URL,
                                      "spec.subprotocol": "postgresql"})
    assert ok.status_code == 200 and "Connected supabase" in ok.text and "New pipeline" in ok.text
    assert PW not in ok.text
    depot = fake.store[("depot", "supabase")]
    assert depot["spec"]["spec"] == {"subprotocol": "postgresql", "host": "aws-0-ap-south-1.pooler.supabase.com",
                                     "port": 5432, "database": "postgres", "params": {"sslmode": "require"}}
    assert '"supabase"' in ok.text                                                        # preselected source
    listed = c.get("/sources").text
    assert "dapp-src-supabase" in listed and "never shown again" in listed and PW not in listed

    fields = c.post("/sources/new", data={"csrf": csrf, "type": "postgresql", "name": "otherpg",
                                          "spec.host": "db.example.com", "spec.port": "5432", "spec.database": "app",
                                          "spec.subprotocol": "postgresql", "spec.params": "sslmode=require",
                                          "secret.username": "reader", "secret.password": "pw-123456"})
    assert "Connected otherpg" in fields.text and fake.store[("depot", "otherpg")]["spec"]["spec"]["port"] == 5432

    rep = c.post("/sources/supabase/creds", data={"csrf": csrf, "secret.username": "u", "secret.password": "rotated-9x"})
    assert "Replaced the credentials of supabase" in rep.text and "rotated-9x" not in rep.text
    assert "Type supabase to confirm" in c.post("/sources/supabase/delete", data={"csrf": csrf, "confirm": "x"}).text
    gone = c.post("/sources/supabase/delete", data={"csrf": csrf, "confirm": "supabase"})
    assert "Removed supabase and its credentials" in gone.text and ("secret", "dapp-src-supabase") not in fake.store
    assert PW not in json.dumps([v for v in fake.store.values() if v.get("type") != "secret"])
