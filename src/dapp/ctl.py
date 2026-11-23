"""Write path: apply/delete resources through `dataos-ctl` (or the Poros API when ctl isn't available).

dataos-ctl does lint, $ENV interpolation, stack resolution and owner defaults, so it's the default for
people and agents. Manifests go over stdin (`resource apply --in`) so secret values never touch disk;
that also works with the dp-toolbox shim, which only sees files under its mounted workspace.

Set DAPP_WRITER=api to skip dataos-ctl and call Poros directly (used inside the dashboard pod).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .client import DataOS, DataOSError
from .spec import dump_yaml


class WriteError(RuntimeError):
    pass


CONFLICT = "has been modified"  # Kubernetes optimistic locking: the operator updated the status as we applied


def with_conflict_retry(fn, *, tries: int = 4, pause: float = 1.5):
    """Re-run a write that lost a race with the operator. Any other error is raised straight away."""
    for attempt in range(tries):
        try:
            return fn()
        except (WriteError, DataOSError) as e:
            if CONFLICT not in str(e) or attempt == tries - 1:
                raise
            time.sleep(pause * (attempt + 1))


class Writer(Protocol):
    name: str

    def apply(self, *manifests: dict[str, Any], lint: bool = False) -> str: ...

    def delete(self, type: str, name: str, *, cascade: bool = False) -> str: ...


def find_ctl() -> str | None:
    """DATAOS_CTL wins; then the binary dataos-ctl installs into its config dir; then PATH.

    The config-dir binary goes before PATH because some PATH entries are wrapper shims (e.g. the dp-toolbox
    one) that need a container to be running.
    """
    explicit = os.environ.get("DATAOS_CTL")
    if explicit:
        return explicit
    from .config import config_dir

    local = config_dir() / "bin" / "dataos-ctl"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("dataos-ctl")


@dataclass
class CtlWriter:
    binary: str = "dataos-ctl"
    timeout: int = 300
    name: str = "dataos-ctl"

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        env = {**os.environ, "LOG_LEVEL": os.environ.get("LOG_LEVEL", "info")}
        try:
            p = subprocess.run(
                [self.binary, *args], input=stdin, capture_output=True, text=True, timeout=self.timeout, env=env
            )
        except FileNotFoundError as e:
            raise WriteError(f"{self.binary} not found on PATH; install it or set DAPP_WRITER=api") from e
        except subprocess.TimeoutExpired as e:
            raise WriteError(f"{self.binary} {' '.join(args)} timed out after {self.timeout}s") from e
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0:
            hint = ""
            if p.returncode == 90:
                hint = " (not logged in: run `dataos-ctl login`)"
            elif p.returncode == 91:
                hint = " (dataos-ctl not initialised: run `dataos-ctl init`)"
            raise WriteError(f"{self.binary} {' '.join(args[:3])} failed (exit {p.returncode}){hint}:\n{out}")
        return out

    def apply(self, *manifests: dict[str, Any], lint: bool = False) -> str:
        args = ["resource", "apply", "--in", "--disable-interpolation"]
        if lint:
            args.append("--lint")
        else:
            args.append("--unquarantine")  # a redeploy means "try again": Poros quarantines failed resources
        stdin = dump_yaml(*manifests)
        return with_conflict_retry(lambda: self._run(args, stdin=stdin))

    def delete(self, type: str, name: str, *, cascade: bool = False) -> str:
        args = ["resource", "delete", "-t", type, "-n", name]
        if cascade:
            args.append("--cascade")
        return with_conflict_retry(lambda: self._run(args))


@dataclass
class ApiWriter:
    client: DataOS
    name: str = "poros-api"

    def apply(self, *manifests: dict[str, Any], lint: bool = False) -> str:
        if lint:
            return "lint skipped: the API writer has no lint mode (Poros validates on apply)"
        done = []
        for m in manifests:
            with_conflict_retry(lambda m=m: self.client.apply(m))
            done.append(f"applied {m['type']}/{m['name']}")
        return "\n".join(done)

    def delete(self, type: str, name: str, *, cascade: bool = False) -> str:
        found = self.client.delete_resource(type, name, cascade=cascade)
        return f"deleted {type}/{name}" if found else f"{type}/{name} not found"


def make_writer(client: DataOS, kind: str | None = None) -> Writer:
    kind = kind or os.environ.get("DAPP_WRITER")
    if kind == "api":
        return ApiWriter(client)
    binary = find_ctl()
    if binary and (kind == "ctl" or kind is None):
        return CtlWriter(binary=binary)
    if kind == "ctl":
        raise WriteError("dataos-ctl not found; set DATAOS_CTL or install it")
    return ApiWriter(client)
