"""Keep the terminal's folders in the owner's immortal-obj volume objects, so a restart loses (almost) nothing.

    ~/work               <-> user:<owner>/workspace          the code being built
    ~/.claude/projects   <-> user:<owner>/claude-sessions    Claude's conversations (for `claude --continue`)

Start: pull both (before the terminal opens). Then push every DAPP_SYNC_SECONDS (default 30) and once more on
SIGTERM, when DataOS stops the pod. Push mirrors the folder (deleted files go away in the object too), so it only
starts after a pull succeeded: a pod that couldn't restore must never wipe the saved copy.

Not synced: caches, virtualenvs, .git, secrets (.env, keys), databases and files over 700 KB, and the Claude
sign-in itself (a credential: sign in again after a restart).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from dapp_objects import Client, ObjectsError

HOME = Path(os.environ.get("HOME", "/tmp/claude-home"))
PAIRS = [(HOME / "work", "workspace"), (HOME / ".claude" / "projects", "claude-sessions")]
EVERY = float(os.environ.get("DAPP_SYNC_SECONDS", "30"))


def say(msg: str) -> None:
    print(f"[workspace-sync] {msg}", file=sys.stderr, flush=True)


def client() -> Client:
    tenant = os.environ.get("DATAOS_TENANT_ID") or os.environ.get("DAPP_TENANT", "")
    gateway = os.environ.get("DAPP_GATEWAY_INTERNAL", "http://dapp-gateway:8080")
    # the pod's owner identity, which DataOS gives it: the gateway checks it with Heimdall
    return Client(f"{gateway}/mcp/{tenant}/immortal-obj", os.environ.get("DATAOS_RUN_AS_APIKEY", ""))


def pull(c: Client) -> bool:
    ok = True
    for folder, name in PAIRS:
        try:
            r = c.volume(name).pull(folder)
            say(f"restored {name}: {r['downloaded']} files")
        except (ObjectsError, OSError) as e:
            say(f"could not restore {name}: {e}")
            ok = False
    return ok


def push(c: Client) -> None:
    for folder, name in PAIRS:
        if not folder.is_dir():
            continue
        try:
            r = c.volume(name).push(folder)
            if r["uploaded"] or r["removed"]:
                say(f"saved {name}: {r['uploaded']} up, {r['removed']} removed"
                    + (f", too big to save: {', '.join(r['skipped'][:5])}" if r["skipped"] else ""))
        except (ObjectsError, OSError) as e:
            say(f"could not save {name}: {e}")


def main() -> int:
    c = client()
    if sys.argv[1:] == ["pull"]:
        return 0 if pull(c) else 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(EVERY):
        push(c)
    push(c)                                              # last save on the way out
    say("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
