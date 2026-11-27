"""The token budget box on an app's LLM tab: the owner sees every user and changes budgets; values are typed as 5M / 200k."""
import re
from typing import ClassVar

import pytest
from test_dashboard import FakeGateway, FakePlatform

from dapp.usage import parse_tokens, show_tokens


def view(can_edit=True):
    return {"tenant": "engineering", "app": "chatbot", "month": "2026-11", "resets_on": "2026-12-01",
            "app_budget": 1_000_000, "default_user_budget": 200_000, "app_spent": 850_000, "app_left": 150_000, "app_pct": 85.0,
            "users": [{"user": "carol", "spent": 210_000, "budget": 200_000, "own_budget": False, "left": 0, "pct": 105.0},
                      {"user": "rajat", "spent": 100_000, "budget": 500_000, "own_budget": True, "left": 400_000, "pct": 20.0}],
            "can_edit": can_edit}


class BudgetGateway(FakeGateway):
    calls: ClassVar[list] = []

    def __init__(self, can_edit=True):
        self.can_edit = can_edit

    def budgets(self, tenant, app):
        return view(self.can_edit)

    def set_budgets(self, tenant, app, *, app_tokens=None, default_user_tokens=None, clear=None):
        self.calls.append(("set", tenant, app, app_tokens, default_user_tokens, clear))
        return {**view(), "note": ""}

    def set_user_budget(self, tenant, app, user, tokens):
        self.calls.append(("user", tenant, app, user, tokens))
        return view()

    def delete_user_budget(self, tenant, app, user):
        self.calls.append(("rm", tenant, app, user))
        return view()


class BudgetPlatform(FakePlatform):
    def status(self, name):
        st = super().status(name)
        st["spec"] = {"service": {"secrets": [{"id": f"engineering:dapp-{name}-llm"}]}}   # the app uses the gateway
        return st

    def resolve_users(self, typed):
        return ["rajat"], {}


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def owner(monkeypatch):
    import test_dashboard
    BudgetGateway.calls = []
    monkeypatch.setattr(test_dashboard, "FakeGateway", lambda: BudgetGateway(True))
    return test_dashboard.signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: BudgetPlatform())


def test_parse_and_show_tokens():
    assert [parse_tokens(x) for x in ("5M", "200k", "2.5m", "50L", "1cr", "1,234", 7)] == \
        [5_000_000, 200_000, 2_500_000, 5_000_000, 10_000_000, 1234, 7]
    assert (show_tokens(5_000_000), show_tokens(250_000), show_tokens(None), show_tokens(12)) == ("5M", "250k", "–", "12")
    with pytest.raises(ValueError):
        parse_tokens("lots")


def test_owner_sees_every_user_with_pills_and_the_form(owner):
    html = owner.get("/apps/chatbot?tab=llm").text
    assert "Token budget · this month" in html and "Resets on" in html and "2026-12-01" in html
    assert '<span class="pill warn">85%</span>' in html                 # the app: 80% and over
    assert '<span class="pill bad">105% · over</span>' in html          # carol: past her budget
    assert '<span class="pill ok">20%</span>' in html and "(own)" in html and "Remove own" in html
    assert 'name="app_tokens" value="1M"' in html and 'name="per_user" value="200k"' in html


def test_owner_changes_budgets(owner):
    token = csrf(owner.get("/apps/chatbot?tab=llm").text)
    r = owner.post("/budgets/chatbot", data={"app_tokens": "5M", "per_user": "", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and "tab=llm" in r.headers["location"] and "Budgets+saved" in r.headers["location"]
    assert BudgetGateway.calls[-1] == ("set", "engineering", "chatbot", 5_000_000, None, ["default_user"])   # cleared field: removed
    owner.post("/budgets/chatbot/users", data={"user": "Rajat@x.io", "tokens": "500k", "csrf": token}, follow_redirects=False)
    assert BudgetGateway.calls[-1] == ("user", "engineering", "chatbot", "rajat", 500_000)
    owner.post("/budgets/chatbot/users/rajat/delete", data={"csrf": token}, follow_redirects=False)
    assert BudgetGateway.calls[-1] == ("rm", "engineering", "chatbot", "rajat")
    r = owner.post("/budgets/chatbot", data={"app_tokens": "lots", "csrf": token}, follow_redirects=False)
    assert "err=" in r.headers["location"] and len(BudgetGateway.calls) == 3
    assert owner.post("/budgets/chatbot", data={"app_tokens": "5M", "csrf": "nope"}, follow_redirects=False).status_code == 403


def test_someone_else_sees_no_form(monkeypatch):
    import test_dashboard
    monkeypatch.setattr(test_dashboard, "FakeGateway", lambda: BudgetGateway(False))
    c = test_dashboard.signed_in_with_fakes(monkeypatch, platform_factory=lambda s, t=None: BudgetPlatform())
    html = c.get("/apps/chatbot?tab=llm").text
    assert "Token budget" in html and "Only the app's owner can change budgets" in html and 'name="app_tokens"' not in html
