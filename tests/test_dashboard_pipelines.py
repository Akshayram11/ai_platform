"""The Pipelines tab through the real routes and the real Platform (DataOS faked at the HTTP level), with access requests kept
in a real immortal-obj server."""
import importlib
import re
import sys
from pathlib import Path

import httpx
from starlette.testclient import TestClient as StarletteClient
from test_dashboard import signed_in_with_fakes
from test_platform_pipelines import Fake

import dapp_objects
from dapp.client import DataOS
from dapp.config import Context
from dapp.ctl import ApiWriter
from dapp.platform import Platform


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_pipelines_tab_end_to_end(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "immortal-obj" / "app"))
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    who = {"me": "alice"}
    fakes = {"alice": Fake("alice", allowed={"pgrds"}), "rohit": Fake("rohit")}
    fakes["rohit"].store = fakes["alice"].store                         # one tenant, two people

    def platform(s, t=None):
        f = fakes[who["me"]]
        client = DataOS(Context("t", "x.dataos.cloud", "ai", who["me"], "key", None), transport=httpx.MockTransport(f))
        return Platform(client, writer=ApiWriter(client))

    with StarletteClient(server.app, base_url="http://immortal-obj:8080") as tc:
        def store():
            def opener(req, timeout):
                headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
                headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": "app:dapp"})
                return Resp(tc.post("/mcp", content=req.data, headers=headers).content)
            return dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener)

        c = signed_in_with_fakes(monkeypatch, platform_factory=platform, app_objects_factory=store)
        home = c.get("/pipelines")
        assert home.status_code == 200 and "No pipeline yet" in home.text and 'class="on">Pipelines' in home.text
        for needle in ("pgrds", "lake", "PostgreSQL", "DataOS Lakehouse", "Move from here", "never grants access by itself"):
            assert needle in home.text, needle

        new = c.get("/pipelines/new", params={"source": "pgrds"})
        assert new.status_code == 200 and "I need changes within seconds" in new.text and '"pgrds"' in new.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', new.text).group(1)
        form = {"csrf": csrf, "name": "orders", "source_depot": "pgrds", "source_object": "public.orders",
                "dest_depot": "lake", "how": "changes", "schedule": "hourly", "incremental_key": "updated_at", "primary_key": "id"}

        bad = c.post("/pipelines/new", data={**form, "primary_key": "", "action": "preview"})
        assert bad.status_code == 400 and "identifies a row" in bad.text

        preview = c.post("/pipelines/new", data={**form, "action": "preview"})
        assert preview.status_code == 200 and "Keep up to date pgrds public.orders into lake raw.orders, every hour" in preview.text
        assert "incremental_strategy: merge" in preview.text and ("nilus", "orders") not in fakes["alice"].store

        refused = c.post("/pipelines/new", data={**form, "action": "deploy"})        # alice may not use Depot lake
        assert refused.status_code == 403 and "Access needed first" in refused.text and "Request access" in refused.text
        assert ("nilus", "orders") not in fakes["alice"].store
        assert not any(t == "grant" for t, _ in fakes["alice"].store)              # nothing granted on the way

        sent = c.post("/pipelines/request-access", data={"csrf": csrf, "depot": "lake", "note": "for pipeline orders"})
        assert "Asked for access to Depot lake" in sent.text and "Your access requests" in sent.text
        assert "Access requests for your Depots" not in sent.text                   # alice doesn't own lake

        who["me"] = "rohit"                                                         # lake's owner decides
        page = c.get("/pipelines").text
        assert "Access requests for your Depots" in page and "for pipeline orders" in page
        rid = re.search(r"/pipelines/requests/([0-9a-f]+)/approve", page).group(1)
        ok = c.post(f"/pipelines/requests/{rid}/approve", data={"csrf": csrf})
        assert "alice may now use Depot lake" in ok.text
        grants = [v for (t, _), v in fakes["alice"].store.items() if t == "grant"]
        assert {g["spec"]["policy_use_case_id"] for g in grants} == {"use-specific-depot", "use-specific-secret"}
        assert all(g["spec"]["subjects"] == ["users:id:alice"] for g in grants)
        assert "no longer open" in c.post(f"/pipelines/requests/{rid}/approve", data={"csrf": csrf}).text

        who["me"] = "alice"
        fakes["alice"].allowed.add("lake")                                          # what the grant did in DataOS
        done = c.post("/pipelines/new", data={**form, "action": "deploy"})
        assert done.status_code == 200 and "Started: Keep up to date" in done.text and "Runs" in done.text
        assert fakes["alice"].store[("nilus", "orders")]["spec"]["schedule"]["crons"] == ["0 * * * *"]
        detail = c.get("/pipelines/orders").text
        for needle in ("completed", "31", "16.8", "Run again now", "Delete pipeline", "0 * * * *", "type: nilus"):
            assert needle in detail, needle
        assert "orders" in c.get("/pipelines").text

        again = c.post("/pipelines/orders/run", data={"csrf": csrf})
        assert "Running again" in again.text and ("nilus", "orders") in fakes["alice"].store
        assert "Type orders to confirm" in c.post("/pipelines/orders/delete", data={"csrf": csrf, "confirm": "x"}).text
        gone = c.post("/pipelines/orders/delete", data={"csrf": csrf, "confirm": "orders"})
        assert "Deleted orders" in gone.text and ("nilus", "orders") not in fakes["alice"].store
        assert c.get("/pipelines/orders").status_code == 404
