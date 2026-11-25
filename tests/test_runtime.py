import os
import sys
from pathlib import Path

import pytest

from dapp.pack import MAX_PACKED, PackError, pack
from dapp.spec import AppSpec, load_spec, render_app

ROOT = Path(__file__).resolve().parents[1]
BASE = {"name": "rt", "description": "d", "compute": "c", "public": True}


def make_code(tmp_path):
    d = tmp_path / "code"
    (d / "pkg").mkdir(parents=True)
    (d / "app.py").write_text("print('hi')\n")
    (d / "pkg" / "util.py").write_text("X = 1\n")
    (d / ".env").write_text("OPENAI_API_KEY=sk-secret\n")          # must never be packed
    (d / ".env.example").write_text("OPENAI_API_KEY=\n")            # fine to ship
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("x")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "a.pyc").write_bytes(b"\0")
    return d


def test_pack_skips_secrets_and_is_deterministic(tmp_path):
    d = make_code(tmp_path)
    a = pack(d)
    assert a.files == [".env.example", "app.py", "pkg/util.py"]
    os.utime(d / "app.py", (1, 1))                     # a new mtime alone doesn't change the version
    assert pack(d).sha256 == a.sha256
    (d / "app.py").write_text("print('changed')\n")
    assert pack(d).sha256 != a.sha256
    assert set(a.data) == {"DAPP_CODE_0", "DAPP_CODE_SHA256"}


def test_pack_limits(tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    (big / "data.bin").write_bytes(os.urandom(MAX_PACKED + 10))
    with pytest.raises(PackError, match="data.bin"):
        pack(big)
    with pytest.raises(PackError, match="does not exist"):
        pack(tmp_path / "nope")


def test_spec_rules(tmp_path):
    ok = {**BASE, "runtime": "python-3.12", "code": "./code", "run": "python app.py"}
    with pytest.raises(ValueError, match="either image"):
        AppSpec(**BASE)
    with pytest.raises(ValueError, match="either image"):
        AppSpec(**ok, image="i:1")
    with pytest.raises(ValueError, match="runtime must be one of"):
        AppSpec(**{**ok, "runtime": "python-2.7"})
    with pytest.raises(ValueError, match="needs code"):
        AppSpec(**{**ok, "run": None})
    private = render_app(AppSpec(**{**ok, "public": False}), tenant="t", user_id="alice",
                         fqdn="x.dataos.cloud")["spec"]["service"]      # private runtime apps: sign-in via dapp_proxy
    assert private["envs"]["DAPP_AUTH"] == "oidc" and private["http"]["oidcClient"]["enabled"]
    with pytest.raises(ValueError, match="are for runtime"):
        AppSpec(**BASE, image="i:1", command=["x"], run="y")
    m = render_app(AppSpec(**ok), tenant="t", user_id="alice")["spec"]["service"]
    assert m["image"].endswith("/dapp-runtime:python-3.12-r8") and m["command"] == ["python", "/opt/dapp/dapp_run.py"]
    assert m["envs"]["DAPP_RUN"] == "python app.py" and {"id": "dapp-rt-code"} in m["secrets"]


def test_load_spec_resolves_code_next_to_the_yaml(tmp_path):
    d = make_code(tmp_path)
    (tmp_path / "dapp.yaml").write_text(
        "name: rt\ndescription: d\ncompute: c\npublic: true\nruntime: python-3.12\ncode: ./code\nrun: python app.py\n")
    assert load_spec(tmp_path / "dapp.yaml").code == str(d.resolve())


def test_launcher_unpacks_checks_and_runs(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "runtimes"))
    import dapp_run

    d = make_code(tmp_path)
    p = pack(d)
    env = {**p.data, "DAPP_RUN": "python app.py"}
    dest = tmp_path / "out"
    assert dapp_run.unpack(env, dest) == p.size
    assert (dest / "pkg" / "util.py").read_text() == "X = 1\n" and not (dest / ".env").exists()
    with pytest.raises(SystemExit, match="hash mismatch"):
        dapp_run.unpack({**env, "DAPP_CODE_SHA256": "0" * 64}, tmp_path / "x")
    with pytest.raises(SystemExit, match="not contiguous"):
        dapp_run.unpack({"DAPP_CODE_1": "x", "DAPP_CODE_SHA256": "y"}, tmp_path / "y")
    with pytest.raises(SystemExit, match="no DAPP_CODE_0"):
        dapp_run.unpack({}, tmp_path / "z")

    monkeypatch.setattr(dapp_run, "CODE", tmp_path / "run" / "code")
    monkeypatch.setattr(os, "environ", {**env, "DAPP_METRICS": "0", "PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                        "DAPP_CODE_VERSION": "abc",
                                        "DAPP_RUN": f"{sys.executable} -c \"import os, pkg.util, sys; "
                                                    "sys.exit(7 if os.environ.get('DAPP_CODE_VERSION') == 'abc' "
                                                    "and 'DAPP_CODE_0' not in os.environ else 1)\""})
    assert dapp_run.main() == 7      # ran in the code folder, importable, version kept, pieces dropped, exit code


def test_launcher_falls_back_when_home_is_read_only(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "runtimes"))
    import dapp_run

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)                      # like /home/app when DataOS runs us as another uid
    try:
        assert dapp_run.writable(str(locked / "code"), str(tmp_path / "tmp")) == tmp_path / "tmp"
    finally:
        locked.chmod(0o700)
