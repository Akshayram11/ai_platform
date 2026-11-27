"""`dapp budget show|set|rm`: an app's monthly token budgets from a terminal (owner only; the gateway checks)."""
import json

import pytest
from typer.testing import CliRunner

from dapp import cli
from dapp.cli import app as cli_app
from dapp.platform import PlatformError

runner = CliRunner()
VIEW = {"tenant": "engineering", "app": "chatbot", "month": "2026-11", "resets_on": "2026-12-01", "app_budget": 5_000_000,
        "default_user_budget": None, "app_spent": 4_100_000, "app_left": 900_000, "app_pct": 82.0, "can_edit": True,
        "users": [{"user": "rajat", "spent": 600_000, "budget": 500_000, "own_budget": True, "left": 0, "pct": 120.0},
                  {"user": "carol", "spent": 10, "budget": None, "own_budget": False, "left": None, "pct": None}]}


class FakePlatform:
    def __init__(self):
        self.calls = []

    def budgets(self, app):
        if app == "nope":
            raise PlatformError("no app nope")
        return VIEW

    def set_budgets(self, app, app_tokens=None, per_user=None, clear=None):
        self.calls.append(("set", app, app_tokens, per_user, clear))
        return VIEW

    def set_user_budget(self, app, user, tokens):
        self.calls.append(("user", app, user, tokens))
        return VIEW

    def delete_user_budget(self, app, user):
        self.calls.append(("rm", app, user))
        return VIEW


@pytest.fixture
def fake(monkeypatch):
    f = FakePlatform()
    monkeypatch.setattr(cli, "_platform", lambda: f)
    return f


def run(*args):
    return runner.invoke(cli_app, list(args))


def test_show(fake):
    r = run("budget", "show", "chatbot")
    assert r.exit_code == 0
    for needle in ("5M", "4.1M", "rajat", "500k (own)", "app's", "2026-12-01"):
        assert needle in r.stdout, needle
    assert json.loads(run("--json", "budget", "show", "chatbot").stdout)["app_budget"] == 5_000_000
    assert run("budget", "show", "nope").exit_code != 0


def test_set_parses_amounts(fake):
    assert run("budget", "set", "chatbot", "--app", "5M", "--per-user", "2lakh").exit_code == 0
    assert fake.calls[-1] == ("set", "chatbot", 5_000_000, 200_000, None)
    assert run("budget", "set", "chatbot", "--user", "rajat", "500k").exit_code == 0
    assert fake.calls[-1] == ("user", "chatbot", "rajat", 500_000)
    r = run("budget", "set", "chatbot")
    assert r.exit_code != 0 and "say what to set" in r.output
    assert run("budget", "set", "chatbot", "--app", "lots").exit_code != 0 and len(fake.calls) == 2


def test_rm(fake):
    assert run("budget", "rm", "chatbot", "--app", "--per-user").exit_code == 0
    assert fake.calls[-1] == ("set", "chatbot", None, None, ["app", "default_user"])
    assert run("budget", "rm", "chatbot", "--user", "rajat").exit_code == 0
    assert fake.calls[-1] == ("rm", "chatbot", "rajat")
    assert run("budget", "rm", "chatbot").exit_code != 0
