"""The Apps page also lists DataOS Services applied on their own (read-only), under the app inventory."""
from test_dashboard import FakePlatform, signed_in_with_fakes

from dapp.platform import Platform

SERVICES = [{"name": "dapp-gateway-core", "owner": "rajat", "description": "the gateway", "state": "running", "status": "active",
             "runtime": "running:1", "error": None, "address": "dapp-gateway-core.poliks-0-engineering-0-ets", "ports": [8080],
             "created": None},
            {"name": "pg", "owner": "rajat", "description": None, "state": "failed", "status": "active",
             "runtime": "failed", "error": "image pull", "address": None, "ports": [], "created": None}]


class ServicePlatform(FakePlatform):
    def list_services(self, everyone=False):
        return SERVICES


class NoServicePlatform(FakePlatform):
    def list_services(self, everyone=False):
        return []


def test_services_are_listed_under_the_apps(monkeypatch):
    c = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: ServicePlatform())
    html = c.get("/").text
    assert 'id="services-title"' in html and "dapp-gateway-core" in html
    assert "dapp-gateway-core.poliks-0-engineering-0-ets" in html and "8080" in html
    assert "image pull" in html                                               # a failed one says why


def test_no_services_no_section(monkeypatch):
    html = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: NoServicePlatform()).get("/").text
    assert "services-title" not in html


def test_a_platform_error_does_not_break_the_page(monkeypatch):
    class Broken(FakePlatform):
        def list_services(self, everyone=False):
            raise RuntimeError("boom")
    c = signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: Broken())
    r = c.get("/")
    assert r.status_code == 200 and "services-title" not in r.text


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def list_resources(self, type, **kw):
        assert type == "service"
        return self.rows


def test_list_services_reads_poros_rows():
    p = Platform.__new__(Platform)
    p.ctx = type("C", (), {"user_id": "rajat"})()
    p.client = FakeClient([
        {"name": "b", "owner": "rajat", "status": "active", "runtime": "running:1",
         "activeProperties": {"serviceFqdn": "b.ns", "servicePorts": '[{"name":"http","port":8080,"targetPort":8080}]'}},
        {"name": "a", "owner": "rajat", "status": "active", "runtime": None, "activeProperties": {"servicePorts": "not json"}}])
    rows = p.list_services()
    assert [r["name"] for r in rows] == ["a", "b"]                            # sorted
    assert rows[1]["address"] == "b.ns" and rows[1]["ports"] == [8080] and rows[1]["state"] == "running"
    assert rows[0]["ports"] == [] and rows[0]["state"] == "pending"           # bad JSON in the ports is not fatal
