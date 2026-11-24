"""dapp runtime launcher: run an app from source, with no image build.

`dapp deploy` (for an app with `runtime:` and `code:` in dapp.yaml) packs the code folder into a tar.gz, stores it
base64 in the app's DataOS secret `dapp-<app>-code`, split into env-sized pieces:

    DAPP_CODE_0, DAPP_CODE_1, ...   base64 pieces (each under Linux's 128 KiB limit for one env var)
    DAPP_CODE_SHA256                hash of the tar.gz, checked before anything runs
    DAPP_RUN                        the app's command, e.g. "streamlit run app.py --server.port $PORT"

This launcher unpacks it into /home/app/code, installs requirements.txt (if any) on top of the libraries the image
already has, starts the CPU/memory endpoint, then runs DAPP_RUN and stays its parent (so signals and the exit code
pass through). $PORT in DAPP_RUN is the port the app must listen on.

Private apps (DAPP_AUTH=oidc): the app listens on 8081 and dapp_proxy serves 8080 with DataOS sign-in in front of
it (pages and websockets), so any web server gets sign-in without code changes. Public apps listen on 8080.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

CODE: Path | None = Path(os.environ["DAPP_CODE_DIR"]) if os.environ.get("DAPP_CODE_DIR") else None
VENV_PY = os.environ.get("DAPP_VENV_PYTHON", sys.executable)


def writable(*candidates: str) -> Path:
    """DataOS may run the container as a user other than the image's own, so /home/app can be read-only for us.
    Use the first place we can actually write to."""
    for c in candidates:
        p = Path(c)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".dapp-write-test"
            probe.write_text("")
            probe.unlink()
            return p
        except OSError:
            continue
    raise SystemExit(f"no writable directory among {candidates}")


def say(msg: str) -> None:
    print(f"[dapp-run] {msg}", flush=True)


def pieces(env: dict[str, str]) -> list[str]:
    keys = sorted((k for k in env if k.startswith("DAPP_CODE_") and k[10:].isdigit()), key=lambda k: int(k[10:]))
    if [int(k[10:]) for k in keys] != list(range(len(keys))):
        raise SystemExit(f"code pieces are not contiguous: {keys}")
    return [env[k] for k in keys]


def unpack(env: dict[str, str], dest: Path) -> int:
    parts = pieces(env)
    if not parts:
        raise SystemExit("no DAPP_CODE_0: deploy this app with `code:` in dapp.yaml (dapp deploy packs it)")
    blob = base64.b64decode("".join(parts))
    want = env.get("DAPP_CODE_SHA256", "")
    got = hashlib.sha256(blob).hexdigest()
    if want != got:
        raise SystemExit(f"code hash mismatch (want {want[:12]}, got {got[:12]}): redeploy the app")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # refuses absolute paths, .. and links out of dest
    return len(blob)


def install_requirements(code: Path, work: Path, env: dict[str, str]) -> str | None:
    """Install requirements.txt. Into the image's venv when we may write to it (it already has the common
    libraries, so only the extras are fetched); otherwise into <work>/deps, returned to put on PYTHONPATH."""
    req = code / "requirements.txt"
    if not req.is_file() or not req.read_text().strip():
        return None
    started = time.monotonic()
    venv = Path(VENV_PY).parent.parent
    target = None if os.access(venv / "lib", os.W_OK) else str(work / "deps")
    say(f"installing requirements.txt {'into the image venv' if target is None else 'into ' + target}")
    cmd = ["uv", "pip", "install", "--no-config", "--python", VENV_PY, "--quiet", "-r", str(req)]
    if target:
        cmd += ["--target", target]
    r = subprocess.run(cmd, env=env, cwd=work)  # cwd: uv looks for config there; /home/app may be unreadable
    if r.returncode != 0:
        raise SystemExit("requirements.txt failed to install (see above)")
    say(f"requirements installed in {time.monotonic() - started:.1f}s")
    return target


def main() -> int:
    env = dict(os.environ)
    run = env.get("DAPP_RUN", "").strip()
    if not run:
        raise SystemExit("DAPP_RUN is not set: put `run:` in dapp.yaml")
    work = writable(*(str(p) for p in (CODE.parent if CODE else None,) if p), "/home/app", "/tmp/dapp")
    code = CODE or work / "code"
    home = writable(env.get("HOME", "/home/app"), str(work / "home"))
    env.update(HOME=str(home), UV_CACHE_DIR=str(work / "uv-cache"), XDG_CACHE_HOME=str(work / "cache"))
    size = unpack(env, code)
    say(f"code {env.get('DAPP_CODE_SHA256', '')[:12]} unpacked ({size} bytes) into {code} (uid {os.getuid()})")
    deps = install_requirements(code, work, env)

    if env.get("DAPP_METRICS", "1") == "1":
        try:
            import dapp_metrics

            dapp_metrics.start()
        except Exception as e:  # metrics are nice to have, never a reason not to start
            say(f"metrics not started: {e}")

    # the app needn't carry its own packed copy (pieces and hash); DAPP_CODE_VERSION stays
    child_env = {k: v for k, v in env.items()
                 if not (k.startswith("DAPP_CODE_") and (k[10:].isdigit() or k == "DAPP_CODE_SHA256"))}
    private = env.get("DAPP_AUTH") == "oidc"
    public_port = env.get("PORT", "8080")
    child_env["PORT"] = "8081" if private else public_port
    child_env["PYTHONPATH"] = os.pathsep.join(p for p in (str(code), deps, child_env.get("PYTHONPATH", "")) if p)
    argv = [a.replace("$PORT", child_env["PORT"]).replace("${PORT}", child_env["PORT"]) for a in shlex.split(run)]
    say(f"starting: {' '.join(argv)}")
    procs = [subprocess.Popen(argv, cwd=code, env=child_env)]
    if private:
        proxy_env = {**env, "DAPP_UPSTREAM": "http://127.0.0.1:8081", "DAPP_METRICS": "0"}
        say(f"DataOS sign-in in front of the app: proxy on :{public_port} -> :8081")
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "dapp_proxy:app", "--host", "0.0.0.0",
                                       "--port", public_port, "--proxy-headers", "--log-level", "warning"],
                                      env=proxy_env, cwd=str(Path(__file__).parent)))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda s, _f: [p.send_signal(s) for p in procs])
    while True:  # whichever stops first (app or proxy) ends the container, so DataOS restarts it whole
        for p in procs:
            code_ = p.poll()
            if code_ is not None:
                for other in procs:
                    if other is not p and other.poll() is None:
                        other.terminate()
                return code_
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
