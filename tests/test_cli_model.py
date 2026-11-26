"""`dapp model ...`: the same models as the dashboard's Models tab, from a terminal (or an agent)."""

import json

import pytest
from typer.testing import CliRunner

from dapp import cli, models
from dapp.cli import app as cli_app
from dapp.platform import PlatformError

runner = CliRunner()


class FakePlatform:
    def __init__(self):
        self.calls = []
        self.ready_after = 0
        self.listed = [{"name": "qwen-x", "owner": "alice", "app": "model-qwen-x", "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M",
                        "source": "dashboard", "ready": True},
                       {"name": "fixed", "owner": "", "app": "llm-fixed", "repo": "", "source": "config", "ready": False}]

    def models(self):
        self.ready_after -= 1
        return [{**m, "ready": m["ready"] and self.ready_after <= 0} if m["name"] == "my-q" else m for m in self.listed]

    def deploy_model(self, d, *, compute, dataplane):
        self.calls.append(("deploy", d.name, d.source, compute, dataplane, d.threads, d.ctx, d.cpu_limit))
        self.listed.append({"name": d.name, "owner": "alice", "app": d.app, "repo": d.source, "source": "dashboard", "ready": True})
        return {"name": d.name, "app": d.app, "volume": d.volume, "secret": d.secret, "source": d.source, "url": "https://x/", "notes": []}

    def delete_model(self, name):
        self.calls.append(("delete", name))
        if name == "nope":
            raise PlatformError("no model nope in engineering")
        return {"name": name, "app": f"model-{name}", "volume": f"model-{name}-data", "output": ""}

    def delete_model_volume(self, volume, timeout=0):
        self.calls.append(("volume", volume))
        return True


@pytest.fixture
def fake(monkeypatch):
    f = FakePlatform()
    monkeypatch.setattr(cli, "_platform", lambda: f)
    monkeypatch.setattr(models, "hf_quants", lambda repo, **k: {"Q4_K_M": 0.46, "Q8_0": 0.63})    # no network in tests
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    monkeypatch.delenv("DAPP_DEFAULT_COMPUTE", raising=False)
    return f


def run(*args):
    r = runner.invoke(cli_app, list(args))
    return r, (json.loads(r.stdout) if r.stdout.lstrip().startswith(("{", "[")) else None)


def test_catalog_lists_every_model_with_what_to_compare(fake):
    r, rows = run("--json", "model", "catalog")
    assert r.exit_code == 0 and len(rows) == len(models.CATALOG)
    q = next(x for x in rows if x["id"] == "qwen2.5-0.5b")
    assert (q["tier"], q["quality"], q["memory"], q["cpu"], q["download_gb"]) == ("light", 2, "1.5Gi", "2", 0.46)
    assert q["words_per_s"] > 15 and q["cost_day"] > 0 and q["huggingface"].endswith(":Q4_K_M") and "multilingual" in q["good_for"]
    text = runner.invoke(cli_app, ["model", "catalog"]).stdout
    assert "qwen3-4b" in text and "$/day" in text and "dapp model deploy ID" in text


def test_dry_run_shows_the_settings_and_the_cost(fake):
    r, plan = run("--json", "model", "deploy", "qwen2.5-1.5b", "--name", "my-q", "--dry-run", "--set", "ctx=8192",
                  "--set", "temperature=0.3", "--set", "flash-attn=on", "--cpu-limit", "3", "--memory-limit", "3Gi")
    assert r.exit_code == 0 and fake.calls == []                                    # nothing deployed
    assert plan["name"] == "my-q" and plan["app"] == "model-my-q" and plan["weights"].endswith("Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M")
    assert plan["cpu"] == "1 (up to 3)" and plan["memory"] == "2Gi (up to 3Gi)" and plan["context"] == 8192
    assert plan["estimated_cost"].startswith("$") and "/day" in plan["estimated_cost"] and "words/s" in plan["speed"]
    env = plan["server_env"]
    assert env["LLAMA_ARG_CTX_SIZE"] == "8192" and env["LLAMA_ARG_TEMPERATURE"] == "0.3" and env["LLAMA_ARG_FLASH_ATTN"] == "on"
    assert env["LLAMA_ARG_THREADS"] == "3" and env["LLAMA_ARG_TOP_K"] == "20"       # threads follow the CPU limit; the model's own sampling stays


@pytest.mark.parametrize("args, message", [
    (["model", "deploy", "qwen9"], "unknown model"), (["model", "deploy"], "choose a model"),
    (["model", "deploy", "qwen2.5-0.5b", "--set", "ctx"], "key=value"), (["model", "deploy", "qwen2.5-0.5b", "--set", "bogus=1"], "bogus"),
    (["model", "deploy", "qwen2.5-0.5b", "--memory-limit", "300Mi", "--memory", "256Mi"], "too small to load"),
    (["model", "deploy", "qwen2.5-0.5b", "--name", "gpt-4o"], "provider's own prefix"),
    (["model", "deploy", "qwen2.5-0.5b", "--quant", "Q2_Z"], "offers: Q4_K_M, Q8_0"), (["model", "deploy", "--hf", "nonsense", "--name", "mine"], "custom model"),
    (["model", "deploy", "--hf", "a/b:Q4_K_M"], "needs a name"),
    (["model", "rm", "x"], "refusing to delete without --yes")])
def test_a_mistake_is_one_clear_sentence(fake, args, message):
    r, out = run("--json", *args)
    assert r.exit_code == 1 and message in out["error"] and fake.calls == []


def test_deploy_uses_the_defaults_then_waits_until_ready(fake, monkeypatch):
    monkeypatch.setenv("DAPP_DEFAULT_COMPUTE", "my-compute")
    fake.ready_after = 3                                      # the model needs a few polls to load
    r = runner.invoke(cli_app, ["model", "deploy", "qwen2.5-0.5b", "--name", "my-q", "--timeout", "60"])
    assert r.exit_code == 0 and fake.calls == [("deploy", "my-q", "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M", "my-compute", "poldp01", 2, 8192, "2")]
    assert "deployed model my-q" in r.stdout and "ready" in r.stdout and "estimated cost $" in r.stdout and "dapp llm-key personal" in r.stdout
    assert fake.ready_after <= 0                              # it did poll until the gateway said ready
    r, done = run("--json", "model", "deploy", "--hf", "Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q8_0", "--name", "mine", "--compute", "c2", "--disk", "4")
    assert r.exit_code == 0 and done["app"] == "model-mine" and done["ready"] is None and fake.calls[-1][3] == "c2"
    assert fake.calls[-1][2].endswith(":Q8_0")


def test_ls_and_rm(fake):
    r, rows = run("--json", "model", "ls")
    assert [m["name"] for m in rows] == ["qwen-x", "fixed"]
    text = runner.invoke(cli_app, ["model", "ls"]).stdout
    assert "qwen-x" in text and "ready" in text and "starting" in text and "(gateway config)" in text
    r, gone = run("--json", "model", "rm", "qwen-x", "--yes")
    assert r.exit_code == 0 and gone["volume_deleted"] is True and fake.calls[-2:] == [("delete", "qwen-x"), ("volume", "model-qwen-x-data")]
    r, gone = run("--json", "model", "rm", "qwen-x", "--yes", "--keep-volume")
    assert gone["volume_deleted"] is None and fake.calls[-1] == ("delete", "qwen-x")
    r, out = run("--json", "model", "rm", "nope", "--yes")
    assert r.exit_code == 1 and "no model nope" in out["error"]


def test_init_writes_a_chatbot_for_the_chosen_model(tmp_path, monkeypatch):
    import yaml

    from dapp.spec import AppSpec

    monkeypatch.setenv("DATAOS_FQDN", "x.dataos.cloud")
    monkeypatch.setenv("DATAOS_TENANT_ID", "engineering")
    local, openai = tmp_path / "local.yaml", tmp_path / "openai.yaml"
    assert runner.invoke(cli_app, ["init", "lowcost-bot", "--template", "chatbot", "--model", "small", "-f", str(local)]).exit_code == 0
    spec = yaml.safe_load(local.read_text())
    AppSpec.model_validate(spec)
    assert spec["env"]["OPENAI_MODEL"] == "small" and "OPENAI_REASONING_EFFORT" not in spec["env"]      # not an OpenAI model
    assert spec["llm"] is True and spec["objects"] is True and spec["image"].startswith("docker.io/tmdcio/dapp-chatbot:")
    assert runner.invoke(cli_app, ["init", "oa-bot", "--template", "chatbot", "-f", str(openai)]).exit_code == 0
    spec = yaml.safe_load(openai.read_text())
    assert spec["env"]["OPENAI_MODEL"] == "gpt-6-luna" and spec["env"]["OPENAI_REASONING_EFFORT"] == "low"   # the default is unchanged


def test_the_agent_guide_only_uses_commands_that_exist(fake):
    """apps/claude-code/CLAUDE.md is what the online Claude follows: every `dapp ...` line in it must be a real command."""
    import re
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[1] / "apps" / "claude-code" / "CLAUDE.md").read_text()
    # a command is `dapp ...` at the start of a line, after a backtick, `&& `, `| ` or `: ` (not "dapp" in prose)
    found = re.findall(r"(?:^[ ]*|[`:|] ?|&& )dapp (?:--json )?((?:(?:model|pipeline|access|keys|llm-key|tool|secret) )?[a-z][\w-]*)", guide, re.MULTILINE)
    seen = {tuple(f.split()) for f in found}
    assert {("model", "catalog"), ("model", "deploy"), ("model", "ls"), ("model", "rm"), ("deploy",), ("init",), ("status",)} <= seen
    for path in seen:
        r = runner.invoke(cli_app, [*path, "--help"])
        assert r.exit_code == 0, f"the guide mentions `dapp {' '.join(path)}`, which doesn't exist"
    for needle in ("low-cost chatbot", "dapp model deploy qwen2.5-0.5b --name small", "dapp init lowcost-bot --template chatbot --model small",
                   "vulcan_query_submit", "dapp pipeline new", "Never try to grant access yourself", "dapp access allow", "Request access", "Models** tab"):
        assert needle in guide, needle
