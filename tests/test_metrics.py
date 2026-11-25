import asyncio
import urllib.request

import dapp_metrics
from dapp.spec import AppSpec, render_app


def test_render_has_prometheus_text():
    text = dapp_metrics.render()
    for name in ("dapp_container_cpu_usage_seconds_total", "dapp_container_memory_working_set_bytes",
                 "dapp_container_cpu_limit_cores", "dapp_container_memory_limit_bytes"):
        assert f"\n{name}{{pod=" in "\n" + text
    cpu = float(next(line.split()[-1] for line in text.splitlines() if line.startswith("dapp_container_cpu_usage")))
    mem = float(next(line.split()[-1] for line in text.splitlines() if line.startswith("dapp_container_memory_work")))
    assert cpu >= 0 and mem > 0


def test_server_serves_only_metrics():
    port = dapp_metrics.start(port=0) or 0
    assert port == 0 or port > 0
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    dapp_metrics.start._port = None
    assert dapp_metrics.start(port=free) == free
    assert b"dapp_container_cpu_usage_seconds_total" in urllib.request.urlopen(f"http://127.0.0.1:{free}/metrics").read()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{free}/secret")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_instrument_counts_requests():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = dapp_metrics.instrument(inner)

    async def run():
        async def send(_): ...
        async def receive(): return {"type": "http.request"}
        await app({"type": "http", "path": "/"}, receive, send)

    asyncio.run(run())
    assert 'dapp_http_requests_total{code="4xx",pod="' in dapp_metrics.render()


def test_metrics_option_sets_metric_endpoint():
    base = {"name": "a", "description": "d", "image": "i", "compute": "c", "command": ["x"], "public": True}
    on = render_app(AppSpec.model_validate({**base, "metrics": True}), tenant="t", user_id="u")
    assert on["spec"]["service"]["metricEndpoint"] == {"port": 9102, "path": "/metrics"}
    off = render_app(AppSpec.model_validate(base), tenant="t", user_id="u")
    assert "metricEndpoint" not in off["spec"]["service"]


def test_prometheus_queries_are_scoped_to_tenant_and_app():
    import httpx

    from dapp.usage import Prometheus

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["query"])
        return httpx.Response(200, json={"data": {"result": [{"value": [0, "2.5"]}]}})

    p = Prometheus("https://x/observability", headers={"apikey": "k"}, transport=httpx.MockTransport(handler))
    now = p.app_now('my"app', "engineering")
    assert now["cpu_cores"] == 2.5 and now["memory_bytes"] == 2.5
    assert seen and all('tenant="engineering"' in q and 'job_name="my\\"app"' in q for q in seen)
    assert any("changes(dapp_container_uptime_seconds" in q for q in seen)  # only pods that are still alive
    seen.clear()
    p.app_now("chatbot", "engineering", "servicev2alphachatbotsvc-jwyp")   # same-named apps of others stay out
    assert seen and all('service_name="servicev2alphachatbotsvc-jwyp"' in q for q in seen)


def test_service_of_reads_the_runtime():
    from dapp.usage import service_of

    rt = {"nodeDetails": [{"name": "servicev2alphachatbotsvc-jwyp-858db5f84c-sh692",
                           "labels": {"app": "servicev2alphachatbotsvc-jwyp"}}]}
    assert service_of(rt) == "servicev2alphachatbotsvc-jwyp"
    assert service_of({}) is None and service_of(None) is None and service_of({"nodeDetails": [{"labels": {}}]}) is None


def test_app_page_renders_with_and_without_compute():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    def page(compute):
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"", "root_path": "/dapp"}
        s = type("S", (), {"user_id": "alice", "tenant": "engineering", "csrf": "t"})()
        app = {"name": "chatbot", "state": "running", "runtime": "app-service:running:1", "error": None,
               "owner": "alice", "tenant": "engineering", "url": "https://x/chatbot/",
               "spec": {"replicas": 1, "service": {}, "app": {}}, "runtime_detail": {}}
        return TEMPLATES.get_template("app.html").render(
            request=Request(scope), s=s, fqdn="x", flash=None, error=None, app=app, tab="overview", name="chatbot",
            has_prom=True, compute=compute, llm=None, cpu_spark="", mem_spark="")

    full = page({"cpu_cores": 0.0016, "memory_bytes": 49770496.0, "cpu_limit_cores": 0.5,
                 "memory_limit_bytes": 402653184.0, "cpu_core_hours_24h": 0.0001, "requests_24h": 12.0,
                 "errors_24h": 2.0, "pods": 1.0})
    assert "47.5 MiB" in full and "of 384.0 MiB (12%)" in full and "limit 0.50" in full and "2 server errors" in full
    assert "metrics: true" in page({"cpu_cores": None, "memory_bytes": None})
    assert "metrics unavailable" in page({"error": "HTTP 502"})


def test_small_llm_cost_is_not_shown_as_zero():
    from dapp_dashboard.app import TEMPLATES

    html = TEMPLATES.get_template("index.html").render(
        request=type("R", (), {"scope": {"root_path": ""}})(), s=type("S", (), {"user_id": "a", "tenant": "t", "csrf": ""})(),
        fqdn="x", flash=None, error=None, apps=[], err=None, all=False, by_app={}, compute={}, has_prom=False,
        llm={"total": {"input_tokens": 62, "output_tokens": 119, "cost_usd": 0.000066}, "rows": []})
    assert "$0.0001" in html and "$0.00<" not in html


def _scope(path="/"):
    return {"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b"", "root_path": "/dapp"}


def test_keys_page_has_rotate_revoke_and_protects_system_keys():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    s = type("S", (), {"user_id": "alice", "tenant": "engineering", "csrf": "tok"})()
    html = TEMPLATES.get_template("keys.html").render(
        request=Request(_scope("/keys")), s=s, fqdn="x", flash=None, error=None, created=None, llm_error=None,
        keys=[{"name": "ultimately_proud_ostrich_1", "expiration": "2026-12-02T00:00:00Z",
               "description": "poros apikey for runnable runas", "system": True},
              {"name": "my_agent_key", "expiration": "2026-12-20T00:00:00Z", "description": "ci", "system": False}],
        llm_groups=[{"app": "chatbot", "tenant": "engineering", "active": 1, "entries": [
            {"prefix": "dapp_NEW", "created": "2026-11-23T08:01:19.279926", "revoked": False},
            {"prefix": "dapp_OLD", "created": "2026-11-23T07:53:33.699882", "revoked": True}]}])
    assert "/dapp/llm-keys/chatbot/rotate" in html and "/dapp/llm-keys/chatbot/dapp_NEW/revoke" in html
    assert "/dapp/llm-keys/chatbot/dapp_OLD/revoke" not in html          # an already revoked key has no button
    assert "23 Nov 2026, 08:01 UTC" in html and "02 Dec 2026, 00:00 UTC" in html
    assert "managed by DataOS" in html and "/keys/ultimately_proud_ostrich_1/delete" not in html
    assert "/keys/my_agent_key/delete" in html


def test_llm_tab_with_and_without_gateway():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    def page(uses, keys):
        s = type("S", (), {"user_id": "a", "tenant": "t", "csrf": "tok"})()
        app = {"name": "bot", "state": "running", "runtime": "x", "error": None, "owner": "a", "tenant": "t", "url": "u",
               "spec": {"replicas": 1, "service": {}, "app": {}}, "runtime_detail": {}}
        return TEMPLATES.get_template("app.html").render(
            request=Request(_scope()), s=s, fqdn="x", flash=None, error=None, app=app, tab="llm", name="bot",
            has_prom=False, compute=None, llm={"rows": [], "total": {}}, uses_gateway=uses, app_llm_keys=keys)

    assert "llm: true" in page(False, [])
    on = page(True, [{"prefix": "dapp_K1", "created": "2026-11-23T08:01:19", "revoked": False}])
    assert "/dapp/llm-keys/bot/rotate" in on and "dapp-bot-llm" in on and "/dapp/llm-keys/bot/dapp_K1/revoke" in on


def test_system_api_key_cannot_be_deleted_without_force():
    import httpx
    import pytest

    from dapp.client import DataOS
    from dapp.config import Context
    from dapp.platform import Platform, PlatformError

    rows = [{"name": "poros_key", "description": "poros apikey for runnable runas"}, {"name": "mine", "description": "ci"}]
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            deleted.append(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        return httpx.Response(200, json=rows)

    p = Platform(DataOS(Context("t", "x", "ai", "alice", "k", None), transport=httpx.MockTransport(handler)))
    with pytest.raises(PlatformError, match="breaks them"):
        p.delete_key("poros_key")
    with pytest.raises(PlatformError, match="no API key"):
        p.delete_key("nope")
    p.delete_key("mine")
    p.delete_key("poros_key", force=True)
    assert deleted == ["mine", "poros_key"]


def test_usage_by_user_table():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    s = type("S", (), {"user_id": "akshay", "tenant": "t", "csrf": "tok"})()
    app = {"name": "bot", "state": "running", "runtime": "x", "error": None, "owner": "akshay", "tenant": "t", "url": "u",
           "spec": {"replicas": 1, "service": {}, "app": {}}, "runtime_detail": {}}
    rows = [{"end_user": "carol", "requests": 30, "input_tokens": 3000, "output_tokens": 1000, "cost_usd": 0.0008},
            {"end_user": "", "requests": 2, "input_tokens": 100, "output_tokens": 100, "cost_usd": 0.00006},
            {"end_user": "dave", "requests": 10, "input_tokens": 600, "output_tokens": 200, "cost_usd": 0.00016}]
    rows.sort(key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True)   # what the route does
    by_user = {"rows": rows, "total": {"requests": 42, "input_tokens": 3700, "output_tokens": 1300, "cost_usd": 0.00102}}

    def page(**kw):
        return TEMPLATES.get_template("app.html").render(
            request=Request(_scope()), s=s, fqdn="x", flash=None, error=None, app=app, tab="llm", name="bot", has_prom=False,
            compute=None, llm={"rows": [], "total": {}}, uses_gateway=True, app_llm_keys=[], days=7, **kw)

    html = page(by_user=by_user)
    assert "Usage by user" in html and html.index("carol") < html.index("dave") < html.index("(not attributed)")
    assert "80%" in html and "$0.0008" in html and "Total" in html          # carol: 4000 of 5000 tokens
    assert "never what people typed" in html
    assert 'href="?tab=llm&days=30"' in html and "font-weight:600" in html   # the current range is highlighted
    assert "No calls in this period" in page(by_user={"rows": [], "total": {}})
    assert "LLM gateway not reachable" in page(by_user={"error": "boom"})


def test_access_tab_manages_the_allow_list():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    s = type("S", (), {"user_id": "akshay", "tenant": "t", "csrf": "tok"})()
    app = {"name": "bot", "state": "running", "runtime": "x", "error": None, "owner": "akshay", "tenant": "t", "url": "u",
           "spec": {"replicas": 1, "service": {}, "app": {}}, "runtime_detail": {}}

    def page(allow, people=()):
        return TEMPLATES.get_template("app.html").render(
            request=Request(_scope()), s=s, fqdn="x", flash=None, error=None, app=app, tab="access", name="bot",
            has_prom=False, compute=None, llm=None, access=[], allow=allow, people=list(people))

    login = {"app": "bot", "mode": "login", "owners": ["akshay"], "users": ["akshay", "carol", "*"], "roles": ["data-dev"],
             "everyone": True, "extra_users": ["carol"]}
    html = page(login, [{"id": "carol", "name": "Carol K", "type": "person"}])
    assert "always allowed" in html and "/dapp/apps/bot/allow" in html and "/dapp/apps/bot/deny" in html
    assert 'name="value" value="carol"' in html and 'name="value" value="data-dev"' in html and 'name="value" value="*"' in html
    assert 'name="value" value="akshay"' not in html            # owners can never be removed from here
    assert "Anyone signed in to DataOS" in html and '<option value="carol">Carol K</option>' in html
    assert "<details" in html and "Advanced" in html             # the raw Heimdall grants are tucked away
    assert "no list to manage" in page({"app": "bot", "mode": "public", "owners": [], "users": [], "roles": []})
    assert "Add people" not in page({"app": "bot", "mode": "public", "owners": [], "users": [], "roles": []})
    assert "doesn't apply" in page({"app": "bot", "mode": "gateway", "owners": [], "users": [], "roles": []})


def test_by_user_cost_shows_a_lower_bound_when_some_models_have_no_price():
    from fastapi import Request

    from dapp_dashboard.app import TEMPLATES

    s = type("S", (), {"user_id": "a", "tenant": "t", "csrf": "tok"})()
    app = {"name": "bot", "state": "running", "runtime": "x", "error": None, "owner": "a", "tenant": "t", "url": "u",
           "spec": {"replicas": 1, "service": {}, "app": {}}, "runtime_detail": {}}
    rows = [{"end_user": "carol", "requests": 2, "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0004, "cost_partial": True},
            {"end_user": "dave", "requests": 1, "input_tokens": 4, "output_tokens": 1, "cost_usd": 0.0001}]
    html = TEMPLATES.get_template("app.html").render(
        request=Request(_scope()), s=s, fqdn="x", flash=None, error=None, app=app, tab="llm", name="bot", has_prom=False,
        compute=None, llm={"rows": [], "total": {}}, uses_gateway=True, app_llm_keys=[], days=30,
        by_user={"rows": rows, "total": {"requests": 3, "input_tokens": 14, "output_tokens": 6, "cost_usd": 0.0005, "cost_partial": True}})
    assert "≥$0.0004" in html and "≥$0.0005" in html and "≥$0.0001" not in html
