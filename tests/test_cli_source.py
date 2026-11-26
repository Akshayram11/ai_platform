"""`dapp source ...`: connect a source from a terminal. Credentials only from the environment or a file."""
import json

from test_platform_sources import PW, URL, make
from typer.testing import CliRunner

from dapp import cli
from dapp.cli import app as cli_app

runner = CliRunner()


def test_source_commands(monkeypatch):
    p, fake = make()
    monkeypatch.setattr(cli, "_platform", lambda: p)
    monkeypatch.delenv("SUPA_URL", raising=False)
    missing = runner.invoke(cli_app, ["--json", "source", "add", "supabase", "--preset", "supabase", "--url-env", "SUPA_URL"])
    assert missing.exit_code == 1 and "read -s SUPA_URL" in missing.stdout
    bad = runner.invoke(cli_app, ["--json", "source", "add", "x1", "--type", "postgresql", "--set", f"password={PW}"])
    assert bad.exit_code == 1 and "--secret-env" in bad.stdout and PW not in bad.stdout

    monkeypatch.setenv("SUPA_URL", URL)
    r = runner.invoke(cli_app, ["--json", "source", "add", "supabase", "--preset", "supabase", "--url-env", "SUPA_URL"])
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["name"] == "supabase" and out["secret"] == "dapp-src-supabase" and PW not in r.stdout
    assert fake.store[("depot", "supabase")]["spec"]["spec"]["host"] == "aws-0-ap-south-1.pooler.supabase.com"
    assert [s["name"] for s in json.loads(runner.invoke(cli_app, ["--json", "source", "ls"]).stdout)] == ["supabase"]

    monkeypatch.setenv("NEWPW", "rotated-pw")
    monkeypatch.setenv("PGUSER_", "postgres.abcd")
    rr = runner.invoke(cli_app, ["--json", "source", "creds", "supabase", "--secret-env", "username=PGUSER_",
                                 "--secret-env", "password=NEWPW"])
    assert rr.exit_code == 0 and "rotated-pw" not in rr.stdout
    assert runner.invoke(cli_app, ["--json", "source", "rm", "supabase"]).exit_code == 1          # needs --yes
    gone = json.loads(runner.invoke(cli_app, ["--json", "source", "rm", "supabase", "--yes"]).stdout)
    assert gone["secret_deleted"] and ("depot", "supabase") not in fake.store
