import importlib
import io
import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import dapp_objects
from dapp.spec import AppSpec, render_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "immortal-obj" / "app"))
from immortal_obj import Caller, ObjError, Store

ALICE, BOB, BOT = Caller("user:alice"), Caller("user:bob"), Caller("app:chatbot")


def test_objects_are_their_owners(tmp_path):
    s = Store(tmp_path)
    s.put(ALICE, "notes", "todo", ["ship it"])
    s.put(BOB, "notes", "todo", ["sleep"])                  # same name, different owner, different object
    assert s.get(ALICE, "notes", "todo") == ["ship it"] and s.get(BOB, "notes", "todo") == ["sleep"]
    assert s.get(ALICE, "nothing", "x", "dflt") == "dflt"
    with pytest.raises(ObjError, match="not yours"):
        s.get(BOB, "user:alice/notes", "todo")
    with pytest.raises(ObjError, match="not yours"):         # same refusal whether or not it exists
        s.get(BOB, "user:alice/ghost", "todo")
    s.put(BOT, "history", "n", 1)
    assert s.objects(BOT)["own"][0]["name"] == "history" and s.objects(ALICE)["shared_with_you"] == []
    assert s.keys(ALICE, "notes") == ["todo"] and s.delete(ALICE, "notes", "todo") == {"deleted": 1}


def test_sharing_read_and_write(tmp_path):
    s = Store(tmp_path)
    s.sql(ALICE, "crm", "create table leads (name text)")
    s.sql(ALICE, "crm", "insert into leads values (?)", ["acme"])
    s.share(ALICE, "crm", "bob", "read")
    assert s.sql(BOB, "user:alice/crm", "select name from leads")["rows"] == [["acme"]]
    with pytest.raises(ObjError, match="read-only"):
        s.sql(BOB, "user:alice/crm", "insert into leads values ('x')")
    with pytest.raises(ObjError, match="for writing"):
        s.put(BOB, "user:alice/crm", "k", 1)
    with pytest.raises(ObjError, match="not yours"):
        s.share(BOB, "user:alice/crm", "carol")               # only the owner shares
    s.share(ALICE, "crm", "app:chatbot", "write")
    s.put(BOT, "user:alice/crm", "seen", True)
    assert s.get(ALICE, "crm", "seen") is True
    assert s.objects(BOB)["shared_with_you"] == [{"object": "user:alice/crm", "mode": "read"}]
    s.unshare(ALICE, "crm", "bob")
    with pytest.raises(ObjError):
        s.get(BOB, "user:alice/crm", "seen")
    s.drop(ALICE, "crm")
    assert s.objects(ALICE)["own"] == [] and not list((tmp_path / "objects").glob("*.sqlite"))


def test_sql_cannot_leave_its_object(tmp_path):
    s = Store(tmp_path)
    for q in ("attach database '/etc/passwd' as x", "pragma query_only=0", "vacuum into '/tmp/steal.db'",
              "select load_extension('x')"):
        with pytest.raises(ObjError):
            s.sql(ALICE, "o", q)
    r = s.sql(ALICE, "o", "with recursive n(i) as (select 1 union all select i+1 from n limit 600) select i from n")
    assert len(r["rows"]) == 500 and r["truncated"]
    for bad in ("../x", "Notes", "a/b/c/d/e", "", "a//b"):
        with pytest.raises(ObjError):
            s.put(ALICE, bad, "k", 1)


def test_server_and_helper_end_to_end(tmp_path, monkeypatch):
    """The helper an app imports -> (what the gateway adds) -> the MCP server -> the volume."""
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    with TestClient(server.app, base_url="http://immortal-obj:8080") as tc:
        assert tc.post("/mcp", json={}).status_code == 401          # nobody gets in without the gateway's token

        def as_gateway(caller):
            def opener(req, timeout):
                headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
                headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": caller})
                r = tc.post("/mcp", content=req.data, headers=headers)
                assert r.status_code == 200, r.text
                return _Resp(r.content)
            return dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "dapp_key", opener=opener)

        bot = as_gateway("app:chatbot")
        db = bot.open("history")
        db.put("theme", {"dark": True})
        assert db.get("theme") == {"dark": True} and db.get("missing", 7) == 7
        db.sql("create table msgs (who text, text text)")
        db.sql("insert into msgs values (?, ?)", ["dave", "hi"])
        assert db.sql("select * from msgs") == [{"who": "dave", "text": "hi"}]
        dave = bot.open("chat", for_user="dave@corp.io")
        dave.put("last", "hello")
        assert dave.name.startswith("u/dave-corp-io-") and dave.keys() == ["last"]
        assert {o["name"] for o in bot.list()["own"]} == {"history", dave.name}

        alice = as_gateway("alice")
        assert alice.open("history").get("theme") is None             # alice's "history" is a different object
        with pytest.raises(dapp_objects.ObjectsError, match="not yours"):
            alice.open("app:chatbot/history").get("theme")
        db.share("alice")
        assert alice.open("app:chatbot/history").sql("select count(*) as n from msgs") == [{"n": 1}]


def test_volume_objects_hold_files(tmp_path):
    s = Store(tmp_path)
    s.write_file(ALICE, "ws", "src/app.py", b"print(1)")
    s.write_file(ALICE, "ws", "logo.png", bytes(range(256)))
    assert s.read_file(ALICE, "ws", "src/app.py") == b"print(1)"
    assert [f["path"] for f in s.ls(ALICE, "ws")] == ["logo.png", "src/app.py"]
    assert [f["path"] for f in s.ls(ALICE, "ws", "src/")] == ["src/app.py"] and s.ls(ALICE, "nope") == []
    for bad in ("../etc/passwd", "/abs", "a//b", "a/./b", "x\\y", ""):
        with pytest.raises(ObjError):
            s.write_file(ALICE, "ws", bad, b"x")
    with pytest.raises(ObjError, match="in the way"):
        s.write_file(ALICE, "ws", "src/app.py/more", b"x")      # a file can't also be a folder
    with pytest.raises(ObjError, match="in the way"):
        s.write_file(ALICE, "ws", "src", b"x")
    with pytest.raises(ObjError, match="over the"):
        s.write_file(ALICE, "ws", "big.bin", b"0" * 700_001)
    with pytest.raises(ObjError, match="is a volume object"):
        s.put(ALICE, "ws", "k", 1)                              # kinds don't mix
    s.put(ALICE, "kv", "k", 1)
    with pytest.raises(ObjError, match="is a db object"):
        s.write_file(ALICE, "kv", "a.txt", b"x")
    with pytest.raises(ObjError, match="not yours"):
        s.read_file(BOB, "user:alice/ws", "src/app.py")
    s.share(ALICE, "ws", "bob", "read")
    assert s.read_file(BOB, "user:alice/ws", "src/app.py") == b"print(1)"
    with pytest.raises(ObjError, match="for writing"):
        s.write_file(BOB, "user:alice/ws", "evil.py", b"x")
    assert s.remove(ALICE, "ws", "src", recursive=True) == {"removed": 1}
    assert {o["name"]: o["kind"] for o in s.objects(ALICE)["own"]} == {"kv": "db", "ws": "volume"}


def test_folder_push_and_pull_through_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    with TestClient(server.app, base_url="http://immortal-obj:8080") as tc:
        def opener(req, timeout):
            headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
            headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": "alice"})
            return _Resp(tc.post("/mcp", content=req.data, headers=headers).content)
        vol = dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener).volume("workspace")

        work = tmp_path / "work"
        (work / "app" / "__pycache__").mkdir(parents=True)
        (work / "app" / "main.py").write_text("print('hi')")
        (work / "app" / "__pycache__" / "x.pyc").write_bytes(b"junk")
        (work / ".env").write_text("SECRET=1")
        (work / "data.bin").write_bytes(bytes(range(256)))
        (work / "huge.csv").write_bytes(b"0" * 800_000)
        r = vol.push(work)
        assert r["uploaded"] == 2 and r["skipped"] == ["huge.csv"]
        assert sorted(f["path"] for f in vol.ls()) == ["app/main.py", "data.bin"]   # no cache, no .env
        assert vol.push(work)["uploaded"] == 0                                      # nothing changed
        (work / "data.bin").unlink()
        (work / "app" / "main.py").write_text("print('v2')")
        assert vol.push(work) == {"uploaded": 1, "removed": 1, "unchanged": 0, "skipped": ["huge.csv"]}

        fresh = tmp_path / "fresh"                               # e.g. a new pod after a restart
        assert vol.pull(fresh) == {"downloaded": 1, "unchanged": 0}
        assert (fresh / "app" / "main.py").read_text() == "print('v2')"
        assert vol.pull(fresh)["downloaded"] == 0
        assert vol.read_text("app/main.py") == "print('v2')"


def test_claude_workspace_survives_a_restart(tmp_path, monkeypatch):
    """apps/claude-code/workspace-sync.py: pod 1 saves ~/work, pod 2 (fresh /tmp) gets it back."""
    import importlib.util
    monkeypatch.setenv("IMMORTAL_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TOOL_TOKEN", "tool-secret")
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    path = Path(__file__).resolve().parents[1] / "apps" / "claude-code" / "workspace-sync.py"

    with TestClient(server.app, base_url="http://immortal-obj:8080") as tc:
        def opener(req, timeout):
            headers = {k: v for k, v in req.header_items() if k.lower() != "authorization"}
            headers.update({"Authorization": "Bearer tool-secret", "x-dapp-caller": "alice"})
            return _Resp(tc.post("/mcp", content=req.data, headers=headers).content)

        def pod(home):
            monkeypatch.setenv("HOME", str(home))
            spec = importlib.util.spec_from_file_location("workspace_sync", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.client = lambda: dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener)
            return mod

        one = pod(tmp_path / "pod1")
        assert one.pull(one.client())                                    # first start: nothing saved yet
        (tmp_path / "pod1" / "work" / "sales").mkdir(parents=True)
        (tmp_path / "pod1" / "work" / "sales" / "app.py").write_text("import streamlit")
        (tmp_path / "pod1" / ".claude" / "projects" / "p").mkdir(parents=True)
        (tmp_path / "pod1" / ".claude" / "projects" / "p" / "s.jsonl").write_text("{}")
        (tmp_path / "pod1" / ".claude" / ".credentials.json").write_text("secret")   # never leaves the pod
        one.push(one.client())                                           # what the 30 s loop does

        two = pod(tmp_path / "pod2")
        assert two.pull(two.client())
        assert (tmp_path / "pod2" / "work" / "sales" / "app.py").read_text() == "import streamlit"
        assert (tmp_path / "pod2" / ".claude" / "projects" / "p" / "s.jsonl").exists()
        assert not (tmp_path / "pod2" / ".claude" / ".credentials.json").exists()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_objects_true_in_dapp_yaml():
    spec = AppSpec.model_validate({"name": "bot", "description": "d", "image": "i:1", "compute": "c",
                                   "command": ["run"], "objects": True})
    assert spec.llm is True and spec.tools == ["immortal-obj"]
    assert json.dumps(render_app(spec.model_copy(update={"public": True}), tenant="ai", user_id="a"))


def test_helper_waits_out_a_gateway_move():
    """Connection refused or 502/503/504 are retried with a back-off; a sent request that failed otherwise is not."""
    import json
    import urllib.error

    ok = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"result": 7}}}).encode()
    calls, sleeps = [], []

    def opener_for(*steps):
        queue = list(steps)

        def opener(req, timeout):
            calls.append(1)
            step = queue.pop(0) if queue else queue_last
            if isinstance(step, Exception):
                raise step
            return _Resp(step)
        queue_last = steps[-1]
        return opener

    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    c = dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener_for(refused, refused, ok),
                            retry_seconds=30, sleep=sleeps.append)
    assert c.call("get", {"object": "o", "key": "k"}) == 7 and len(calls) == 3 and sleeps == [1.0, 2.0]

    calls.clear(), sleeps.clear()
    unavailable = urllib.error.HTTPError("http://gw", 503, "unavailable", {}, io.BytesIO(b"no healthy upstream"))
    c = dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener_for(unavailable, ok),
                            retry_seconds=30, sleep=sleeps.append)
    assert c.call("get", {"object": "o", "key": "k"}) == 7 and len(calls) == 2

    calls.clear(), sleeps.clear()                                           # still down after the allowed time: gives up
    c = dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener_for(refused), retry_seconds=4,
                            sleep=sleeps.append)
    with pytest.raises(urllib.error.URLError):
        c.call("get", {"object": "o", "key": "k"})
    assert 1 < len(calls) < 6 and len(sleeps) == len(calls) - 1

    calls.clear(), sleeps.clear()                                           # a real refusal is answered at once
    bad = urllib.error.HTTPError("http://gw", 401, "no", {}, io.BytesIO(b"nope"))
    c = dapp_objects.Client("http://gw/mcp/ai/immortal-obj", "k", opener=opener_for(bad), retry_seconds=30,
                            sleep=sleeps.append)
    with pytest.raises(dapp_objects.ObjectsError, match="HTTP 401"):
        c.call("get", {"object": "o", "key": "k"})
    assert len(calls) == 1 and sleeps == []
