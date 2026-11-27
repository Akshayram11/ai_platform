"""Monthly token budgets in the gateway: the app's ceiling, a default per user, one user's own; only the app's owner sets them."""
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient
from test_gateway import FakeHeimdall, dataos, upstream

from dapp_gateway.app import create_app
from dapp_gateway.store import Store, month_of, next_month_start, tokens_of

ALICE, BOB = {"apikey": "alice-key"}, {"apikey": "bob-key"}       # alice owns app chatbot; bob doesn't


def setup(tmp_path, monkeypatch, admins: str = ""):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DAPP_GATEWAY_ADMINS", admins)
    store = Store(f"sqlite:///{tmp_path}/g.db")
    c = TestClient(create_app(store=store, heimdall=FakeHeimdall(), upstream_transport=httpx.MockTransport(upstream),
                              dataos_transport=httpx.MockTransport(dataos)))
    key = c.post("/admin/keys", json={"app": "chatbot", "tenant": "ai"}, headers=ALICE).json()["key"]
    return c, store, key


def ask(c, key, user=None, stream=False):
    h = {"authorization": f"Bearer {key}", **({"x-dapp-user": user} if user else {})}
    return c.post("/openai/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": [], **({"stream": True} if stream else {})},
                  headers=h)                                     # every answer costs 15 tokens (10 in + 5 out)


def test_tokens_count_input_output_and_cache():
    assert tokens_of({"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3, "cache_write_tokens": 2}) == 20
    assert month_of(datetime(2026, 11, 30, 23, 59, tzinfo=UTC)) == "2026-11"
    assert next_month_start("2026-12").isoformat() == "2027-01-01T00:00:00+00:00"


def test_no_budget_no_limit_and_counters_from_the_first_call(tmp_path, monkeypatch):
    c, store, key = setup(tmp_path, monkeypatch)
    r = ask(c, key, "carol")
    assert r.status_code == 200 and "x-dapp-budget-left" not in r.headers
    ask(c, key, "carol", stream=True)                            # streaming: 7 + 2 tokens
    assert store.spent("ai", "chatbot") == 24 and store.spent("ai", "chatbot", "carol") == 24
    ask(c, key)                                                   # no user: counts for the app only
    assert store.spent("ai", "chatbot") == 39 and store.user_spend("ai", "chatbot") == {"carol": 24}


def test_app_budget_is_the_ceiling(tmp_path, monkeypatch):
    c, store, key = setup(tmp_path, monkeypatch)
    v = c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 40}, headers=ALICE).json()
    assert v["app_budget"] == 40 and v["can_edit"] and v["resets_on"] == next_month_start(month_of()).date().isoformat()
    r = ask(c, key, "carol")
    assert r.status_code == 200 and r.headers["x-dapp-budget-left"] == "40"
    assert "x-dapp-user-budget-left" not in r.headers             # no user budget: carol may use the app's whole budget
    assert ask(c, key, "carol").headers["x-dapp-budget-left"] == "25"
    ask(c, key, "dave")                                           # 45 now: this call crossed the line, and finished
    refused = ask(c, key, "erin")
    assert refused.status_code == 429 and refused.headers["x-dapp-budget-left"] == "0"
    err = refused.json()["error"]
    assert err["type"] == "budget_exceeded" and "app chatbot has used its monthly token budget (45 of 40" in err["message"]
    assert "resets on" in err["message"] and store.spent("ai", "chatbot") == 45          # a refused call costs nothing
    c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 1000}, headers=ALICE)        # the owner raises it
    assert ask(c, key, "erin").status_code == 200


def test_user_budgets_default_and_own(tmp_path, monkeypatch):
    c, _, key = setup(tmp_path, monkeypatch)
    c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 1000, "default_user_tokens": 20}, headers=ALICE)
    c.put("/admin/budgets/ai/chatbot/users/rajat", json={"tokens": 40}, headers=ALICE)
    r = ask(c, key, "carol")
    assert r.headers["x-dapp-user-budget-left"] == "20" and r.headers["x-dapp-budget-left"] == "1000"
    ask(c, key, "carol")
    no = ask(c, key, "carol")
    assert no.status_code == 429 and "carol has used their monthly token budget in app chatbot (30 of 20" in no.json()["error"][
        "message"] and no.headers["x-dapp-user-budget-left"] == "0"
    assert ask(c, key, "rajat").headers["x-dapp-user-budget-left"] == "40"            # her own budget, not the default
    assert ask(c, key).status_code == 200                                              # a call with no user: app only
    v = c.get("/admin/budgets/ai/chatbot", headers=ALICE).json()
    rows = {u["user"]: u for u in v["users"]}
    assert rows["carol"] == {"user": "carol", "spent": 30, "budget": 20, "own_budget": False, "left": 0, "pct": 150.0}
    assert rows["rajat"]["own_budget"] and rows["rajat"]["budget"] == 40 and v["app_spent"] == 60
    c.delete("/admin/budgets/ai/chatbot/users/rajat", headers=ALICE)                    # back to the default
    assert c.get("/admin/budgets/ai/chatbot", headers=ALICE).json()["users"][1]["budget"] == 20
    c.put("/admin/budgets/ai/chatbot", json={"clear": ["default_user"]}, headers=ALICE)
    assert ask(c, key, "carol").status_code == 200                                     # no user budget: up to the app's


def test_only_the_owner_changes_budgets_users_see_their_own(tmp_path, monkeypatch):
    c, _, key = setup(tmp_path, monkeypatch, admins="bob")                       # even a gateway admin may not
    assert c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 5}, headers=BOB).status_code == 403
    assert c.put("/admin/budgets/ai/chatbot/users/bob", json={"tokens": 9}, headers=BOB).status_code == 403
    assert c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 5}).status_code == 401
    assert c.put("/admin/budgets/ai/nope", json={"app_tokens": 5}, headers=ALICE).status_code == 404
    assert c.put("/admin/budgets/ai/user:alice", json={"app_tokens": 5}, headers=ALICE).status_code == 400   # personal key
    c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 100, "default_user_tokens": 50}, headers=ALICE)
    ask(c, key, "carol")
    ask(c, key, "bob")
    mine = c.get("/admin/budgets/ai/chatbot", headers=BOB).json()
    assert not mine["can_edit"] and [u["user"] for u in mine["users"]] == ["bob"] and mine["users"][0]["left"] == 35
    assert mine["app_left"] == 70                                                      # the app's totals, not carol's row


def test_a_user_budget_cannot_be_above_the_app_budget(tmp_path, monkeypatch):
    c, _, _ = setup(tmp_path, monkeypatch)
    c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 100}, headers=ALICE)
    r = c.put("/admin/budgets/ai/chatbot/users/rajat", json={"tokens": 500}, headers=ALICE)
    assert r.status_code == 422 and "can't be above the app budget (100)" in r.json()["error"]["message"]
    r = c.put("/admin/budgets/ai/chatbot", json={"default_user_tokens": 200}, headers=ALICE)
    assert r.status_code == 422
    c.put("/admin/budgets/ai/chatbot/users/rajat", json={"tokens": 90}, headers=ALICE)
    v = c.put("/admin/budgets/ai/chatbot", json={"app_tokens": 50}, headers=ALICE).json()   # lowering the ceiling: allowed
    assert "above the app budget, which applies first: rajat" in v["note"]


def test_personal_keys_have_no_budget(tmp_path, monkeypatch):
    c, store, _ = setup(tmp_path, monkeypatch)
    pk = c.post("/admin/keys/personal", json={"tenant": "ai"}, headers=ALICE).json()["key"]
    for _ in range(3):
        assert ask(c, pk).status_code == 200
    assert store.spent("ai", "user:alice") == 45          # usage still recorded (from the usage table); no counter, no budget
