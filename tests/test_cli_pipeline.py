"""`dapp pipeline ...`: the dashboard's Pipelines tab from a terminal (or an agent), on the real Platform."""
import json

from test_platform_pipelines import make
from typer.testing import CliRunner

from dapp import cli
from dapp.cli import app as cli_app
from dapp.config import Context

runner = CliRunner()


def test_pipeline_commands(monkeypatch):
    p, fake = make("alice", allowed={"pgrds"})
    monkeypatch.setattr(cli, "_platform", lambda: p)
    monkeypatch.setattr(cli, "load_context", lambda *a, **k: Context("t", "x.dataos.cloud", "ai", "alice", "k", None))

    kinds = json.loads(runner.invoke(cli_app, ["--json", "pipeline", "kinds"]).stdout)
    assert {k["how"]: k["type"] for k in kinds}["live"] == "cdc"
    depots = json.loads(runner.invoke(cli_app, ["--json", "pipeline", "depots"]).stdout)
    assert {d["name"]: d["modes"] for d in depots}["pgrds"] == ["batch", "cdc", "metadata"]

    args = ["pipeline", "new", "orders", "--from", "pgrds:public.orders", "--to", "lake:raw.orders", "--how", "changes",
            "--incremental-key", "updated_at", "--primary-key", "id", "--schedule", "hourly", "--mask", "email=hash"]
    dry = json.loads(runner.invoke(cli_app, ["--json", *args, "--dry-run"]).stdout)
    assert dry["describe"].startswith("Keep up to date pgrds public.orders into lake raw.orders, every hour")
    assert dry["manifest"]["spec"]["source"]["options"]["mask"] == {"email": "hash"} and ("nilus", "orders") not in fake.store

    refused = runner.invoke(cli_app, ["--json", *args])                # no access to Depot lake: reported, not granted
    assert refused.exit_code == 1
    err = json.loads(refused.stdout)["error"]
    assert "depot lake" in err and "x.dataos.cloud/dapp/pipelines/new" in err and "Nothing was created" in err
    assert not any(t in ("grant", "nilus") for t, _ in fake.store)

    fake.allowed.add("lake")
    ok = json.loads(runner.invoke(cli_app, ["--json", *args]).stdout)
    assert ok["name"] == "orders"
    ls = json.loads(runner.invoke(cli_app, ["--json", "pipeline", "ls"]).stdout)
    assert ls[0]["name"] == "orders" and ls[0]["phase"] == "completed" and ls[0]["when"] == "0 * * * *"
    st = json.loads(runner.invoke(cli_app, ["--json", "pipeline", "status", "orders"]).stdout)
    assert st["runs"][0]["rows"] == 31 and st["stats_7d"]["total_runs"] == 1
    assert "lakehouse" in runner.invoke(cli_app, ["pipeline", "logs", "orders"]).stdout
    assert json.loads(runner.invoke(cli_app, ["--json", "pipeline", "run", "orders"]).stdout) == {"name": "orders"}
    no = runner.invoke(cli_app, ["--json", "pipeline", "rm", "orders"])
    assert no.exit_code == 1 and "--yes" in no.stdout
    assert json.loads(runner.invoke(cli_app, ["--json", "pipeline", "rm", "orders", "--yes"]).stdout)["name"] == "orders"
    assert ("nilus", "orders") not in fake.store
