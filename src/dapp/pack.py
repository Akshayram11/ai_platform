"""Pack an app's source folder for a dapp runtime image (see runtimes/dapp_run.py).

The tar.gz is deterministic (sorted names, no timestamps or owners), so the same code gives the same hash and a
redeploy without changes doesn't restart the app. It travels in a DataOS secret, which Kubernetes caps at 1 MiB,
and as env vars, which Linux caps at 128 KiB each: hence the size limit and the pieces.
"""

from __future__ import annotations

import base64
import fnmatch
import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

MAX_PACKED = 700_000          # bytes of tar.gz; base64 makes it ~933 KB, under the 1 MiB secret limit
PIECE = 100_000               # base64 characters per env var (Linux: 131072 max per variable)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             ".idea", ".vscode", "dist", "build", ".dapp"}
SKIP_FILES = ["*.pyc", ".DS_Store", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.db", "*.sqlite"]
KEEP_FILES = [".env.example"]


class PackError(ValueError):
    pass


@dataclass
class Packed:
    data: dict[str, str]      # DAPP_CODE_0.., DAPP_CODE_SHA256: the secret's keys
    sha256: str
    size: int
    files: list[str]


def _skipped(name: str) -> bool:
    return not any(fnmatch.fnmatch(name, k) for k in KEEP_FILES) and any(fnmatch.fnmatch(name, p) for p in SKIP_FILES)


def collect(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts) or p.is_symlink() or not p.is_file() or _skipped(p.name):
            continue
        out.append(p)
    return out


def pack(folder: str | Path) -> Packed:
    root = Path(folder).resolve()
    if not root.is_dir():
        raise PackError(f"code folder {root} does not exist")
    files = collect(root)
    if not files:
        raise PackError(f"code folder {root} has no files")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in files:
            info = tarfile.TarInfo(p.relative_to(root).as_posix())
            body = p.read_bytes()
            info.size, info.mode, info.mtime = len(body), 0o644, 0
            tar.addfile(info, io.BytesIO(body))
    blob = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    if len(blob) > MAX_PACKED:
        biggest = sorted(files, key=lambda p: p.stat().st_size, reverse=True)[:3]
        raise PackError(
            f"code is {len(blob) // 1024} KB packed; the limit is {MAX_PACKED // 1024} KB. Largest files: "
            + ", ".join(f"{p.relative_to(root)} ({p.stat().st_size // 1024} KB)" for p in biggest)
            + ". Keep data files out of the code folder (read them from DataOS), or build an image instead.")
    text = base64.b64encode(blob).decode()
    sha = hashlib.sha256(blob).hexdigest()
    data = {f"DAPP_CODE_{i}": text[o:o + PIECE] for i, o in enumerate(range(0, len(text), PIECE))}
    data["DAPP_CODE_SHA256"] = sha
    return Packed(data, sha, len(blob), [p.relative_to(root).as_posix() for p in files])
