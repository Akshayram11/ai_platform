"""examples/data-chat: finds data through the Data Product MCP itself and has the small model explain it."""

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

APP = Path(__file__).resolve().parents[1] / "examples" / "data-chat" / "app"

SEARCH = """Found 2 confirmed data products (page 1, limit 5).

1. RR [SP] Retail Intelligence Platform
   arn: arn:engineering/ri-sparkrr
   "Governed retail analytics."
   [domain=retail | status=Active]

2. Postgres DP
   arn: arn:engineering/postgres-dp
   "BI fixture."
"""
SCHEMA = {"schema": """# ri-sparkrr — semantic schema
5 models | 3 measures

## customer_profile

### Measures
| Name | Type | Expression | Description | Instructions | Synonyms |
|---|---|---|---|---|---|
| avg_customer_ltv | avg | customer_profile.total_revenue | Average lifetime revenue per customer | — | — |
| customer_row_count | count | * | Count of customer profile rows | — | customers, how many customers |
| max_customer_ltv | max | customer_profile.total_revenue | Highest lifetime revenue | — | — |

### Dimensions
| Name | Type | Expression | Description | Instructions | Synonyms |
|---|---|---|---|---|---|
| customer_segment | string | customer_profile.customer_segment | RFM segment of the customer | — | segment |
| signup_date | time | customer_profile.signup_date | When the customer signed up | — | — |

## rfm

### Measures
| Name | Type | Expression | Description | Instructions | Synonyms |
|---|---|---|---|---|---|
| rfm_score | avg | rfm.score | Average RFM score | — | — |
"""}


@pytest.fixture
def bot(monkeypatch):
    fake = types.ModuleType("openai")
    fake.OpenAI = object
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.syspath_prepend(str(APP))
    for mod in ("app", "mcp_tools"):
        sys.modules.pop(mod, None)
    monkeypatch.setenv("DAPP_TOOLS", "dataproduct-mcp")
    monkeypatch.setenv("DAPP_TOOLS_URL", "http://gw/mcp/engineering")
    monkeypatch.setenv("DAPP_LLM_KEY", "k")
    monkeypatch.delenv("DAPP_OBJECTS_URL", raising=False)
    import app as module

    module.POLL_SECONDS = 0

    class Llm:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    Llm.sent = kw
                    return iter([NS(choices=[NS(delta=NS(content="Explained."))])])

    monkeypatch.setattr(module, "_client", Llm())
    module.Llm = Llm
    return module


def mcp(replies):
    """A stand-in for the gateway toolbox: answers by function name; a list is consumed in order."""
    calls = []

    class Box:
        def run(self, fn_name, arguments, user):
            short = fn_name.split("__", 1)[1]
            calls.append((short, json.loads(arguments), user))
            r = replies[short]
            return r.pop(0) if isinstance(r, list) else r
    return Box(), calls


def test_schema_is_parsed_and_matched_to_words(bot):
    cat = bot.parse_schema(SCHEMA["schema"])
    assert [m["fq"] for m in cat["measures"]] == [
        "customer_profile.avg_customer_ltv", "customer_profile.customer_row_count",
        "customer_profile.max_customer_ltv", "rfm.rfm_score"]
    assert [d["fq"] for d in cat["dimensions"]] == ["customer_profile.customer_segment", "customer_profile.signup_date"]
    m, d = bot.plan("average customer ltv by segment", cat)
    assert (m["name"], d["name"]) == ("avg_customer_ltv", "customer_segment")
    m, d = bot.plan("how many customers?", cat)
    assert (m["name"], d) == ("customer_row_count", None)
    assert bot.plan("weather in paris", cat) == (None, None)
    m, _ = bot.plan("customer lifetime value", cat)                       # explains 2 of 3 content words: good enough
    assert m["name"] == "avg_customer_ltv"
    assert bot.plan("customer churn probability", cat) == (None, None)    # only "customer" fits: another question, so no query
    assert bot.find_products(SEARCH) == [("RR [SP] Retail Intelligence Platform", "ri-sparkrr"), ("Postgres DP", "postgres-dp")]


def test_answer_runs_the_steps_and_the_model_explains(bot):
    box, calls = mcp({
        "search": SEARCH, "vulcan_semantic_schema": json.dumps(SCHEMA),
        "vulcan_query_submit": '{"statement_id":"S1","status":"QUEUED"}',
        "vulcan_query_result": ['{"statement_id":"S1","status":"IN_PROGRESS"}',
                                json.dumps({"status": "SUCCESS", "row_count": 2, "results": [
                                    {"customer_profile.customer_segment": "gold", "customer_profile.avg_customer_ltv": 900},
                                    {"customer_profile.customer_segment": "new", "customer_profile.avg_customer_ltv": 40}]})]})
    bot.BOX = box
    out = "".join(bot.answer("average customer ltv by segment", "carol"))
    assert "› search(index=data_product, tenant=engineering)" in out and "› found 2 data product(s): ri-sparkrr, postgres-dp" in out
    assert "› vulcan_query_submit(measures=['customer_profile.avg_customer_ltv'], dimensions=['customer_profile.customer_segment'])" in out
    assert "› vulcan_query_result → 2 row(s)" in out and out.endswith("Explained.")
    submit = next(a for n, a, _ in calls if n == "vulcan_query_submit")
    assert submit["dp_name"] == "ri-sparkrr" and submit["tenant"] == "engineering" and submit["limit"] == 20
    assert all(u == "carol" for _, _, u in calls)                           # the gateway learns who asked
    facts = bot.Llm.sent["messages"][1]["content"]
    assert "gold" in facts and "900" in facts and "tools" not in bot.Llm.sent       # numbers reach the model; no tool calling
    assert bot.Llm.sent["model"] == "small" and len(facts) < 6000                   # fits the 4096-token context


def test_next_candidate_is_tried_when_the_first_has_no_matching_measure(bot):
    other = {"schema": "## orders\n\n### Measures\n| Name | Type | Expression | Description | Instructions | Synonyms |\n"
                       "|---|---|---|---|---|---|\n| order_total | sum | orders.total | Sum of order totals | — | — |\n"}
    box, calls = mcp({"search": SEARCH, "vulcan_semantic_schema": [json.dumps(other), json.dumps(SCHEMA)],
                      "vulcan_query_submit": '{"statement_id":"S1"}',
                      "vulcan_query_result": json.dumps({"status": "SUCCESS", "row_count": 1, "results": [{"x": 50000}]})})
    bot.BOX = box
    out = "".join(bot.answer("how many customers?", "carol"))
    assert "› nothing in ri-sparkrr fits the question, trying the next one" in out and "› vulcan_semantic_schema(postgres-dp)" in out
    assert next(a for n, a, _ in calls if n == "vulcan_query_submit")["dp_name"] == "postgres-dp"
    assert "50000" in bot.Llm.sent["messages"][1]["content"] and out.endswith("Explained.")


def test_choosing_a_product_and_questions_that_match_nothing(bot):
    box, calls = mcp({"vulcan_semantic_schema": json.dumps(SCHEMA)})
    bot.BOX = box
    out = "".join(bot.answer("dp:postgres-dp weather in paris", "carol"))
    assert [n for n, _, _ in calls] == ["vulcan_semantic_schema"] and calls[0][1]["dp_name"] == "postgres-dp"   # no search
    assert "I couldn't match that to a measure" in out and "avg_customer_ltv" in out and "customer_segment" in out
    assert not hasattr(bot.Llm, "sent")                    # no number, so the 0.5B model is not asked to improvise one


def test_the_chat_says_so_when_the_data_cannot_be_reached(bot):
    box, _ = mcp({"search": "tool error: dataproduct-mcp: HTTP 502 bad gateway"})
    bot.BOX = box
    out = "".join(bot.answer("revenue?", "carol"))
    assert "couldn't reach the data" in out and "502" in out and not hasattr(bot.Llm, "sent")
    failing = {"search": SEARCH, "vulcan_semantic_schema": [json.dumps(SCHEMA), json.dumps(SCHEMA)],
               "vulcan_query_submit": '{"statement_id":"S1"}',
               "vulcan_query_result": '{"status":"FAILED","error":"no such column"}'}
    box, _ = mcp(failing)
    bot.BOX = box
    out = "".join(bot.answer("how many customers", "carol"))
    assert "[The query on ri-sparkrr failed: no such column]" in out and "[The query on postgres-dp failed" in out   # tries the next
    assert "the query failed on ri-sparkrr, postgres-dp" in out and not hasattr(bot.Llm, "sent")
    bot.BOX = None
    assert "isn't set up" in "".join(bot.answer("anything", "carol"))
    box, _ = mcp({"search": "Found 0 confirmed data products."})
    bot.BOX = box
    assert "No data products were found" in "".join(bot.answer("anything", "carol"))


def test_history_per_person_through_the_endpoints(bot, monkeypatch):
    from test_chatbot_tools import FakeObjects

    objs = FakeObjects()
    monkeypatch.setattr(bot, "objects", objs)
    monkeypatch.setattr(bot, "HISTORY", True)
    monkeypatch.setattr(bot, "answer", lambda q, u: iter(["› step\n", "42 customers."]))
    bot._ready.clear()
    c = TestClient(bot.app)
    carol = {"x-dapp-user": "carol"}
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "how many customers?"}]}, headers=carol)
    cid = r.headers["x-chat-id"]
    assert r.text == "› step\n42 customers."
    got = c.get(f"/api/history/{cid}", headers=carol).json()["messages"]
    assert [(m["role"], m["content"]) for m in got] == [("user", "how many customers?"), ("assistant", "› step\n42 customers.")]
    assert c.get("/api/history", headers={"x-dapp-user": "dave"}).json()["chats"] == []
    assert ("chat", "carol", "carol") in objs.shared
    assert c.post("/api/chat", json={"messages": []}, headers=carol).status_code == 400
    assert c.get("/healthz").json()["mcp"] is True
    assert "Data Chat" in c.get("/").text and "small" in c.get("/").text
