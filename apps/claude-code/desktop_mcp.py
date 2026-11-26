"""The desktop tool for Claude: see the whole screen and use it with the mouse and keyboard (MCP, stdio).

The screen is the one the user watches next to the terminal (Xvfb display :99, shown through noVNC). Claude has two ways in:
  - the `browser` MCP server (Playwright over CDP) for web pages: precise, and it doesn't move the real mouse;
  - this one for everything else on the screen (a dialog, a desktop app, a page the browser tools can't reach): it takes
    screenshots and drives the real mouse and keyboard with xdotool.

The user stays in charge: every action marks Claude as "on the screen" (the page shows it), and when the user presses
"Take over" every action that moves the mouse or types is refused until they hand the screen back. Screenshots stay allowed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DISPLAY = os.environ.get("DESKTOP_DISPLAY", ":99")
ACTIVE, PAUSED = Path("/tmp/desktop-claude-active"), Path("/tmp/desktop-paused")
BASE = "/" + os.environ.get("ROOT_PATH", "").strip("/")
WORKBENCH = os.environ.get("WORKBENCH_URL", "http://127.0.0.1:7680") + ("" if BASE == "/" else BASE)
BUTTONS = {"left": "1", "middle": "2", "right": "3"}
KEY_NAMES = {"enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab", "backspace": "BackSpace",
             "delete": "Delete", "up": "Up", "down": "Down", "left": "Left", "right": "Right", "home": "Home", "end": "End",
             "pageup": "Prior", "pagedown": "Next", "space": "space", "cmd": "super", "win": "super", "control": "ctrl"}

mcp = MCPServer("desktop", instructions=(
    "The user's whole screen (the desktop next to the terminal, the same one they watch). Call screenshot first to see it; "
    "coordinates are pixels of that screenshot. Use the `browser` tools for web pages (they are more precise); use these for "
    "anything else. If an action says the user has taken over, stop and ask them to hand the screen back."))


def _env() -> dict[str, str]:
    return {**os.environ, "DISPLAY": DISPLAY}


def _ensure_desktop() -> None:
    """Start the desktop if it is off (the workbench owns it), and keep it from stopping while Claude uses it."""
    try:
        req = urllib.request.Request(WORKBENCH + "/browser/start", method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=45) as r:
            r.read()
    except OSError as e:
        raise ToolError(f"the desktop could not be started: {e}") from e


def _act() -> None:
    """Before moving the mouse or typing: respect Take over, then say Claude is on the screen."""
    if PAUSED.exists():
        raise ToolError("The user has taken over the screen. Ask them to press 'Give back to Claude' when they are done; "
                        "screenshot still works meanwhile.")
    _ensure_desktop()
    ACTIVE.touch()


def _xdotool(*args: str) -> None:
    if not shutil.which("xdotool"):
        raise ToolError("xdotool is not installed in this machine")
    r = subprocess.run(["xdotool", *args], env=_env(), capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise ToolError(f"xdotool {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")


def _key_combo(keys: str) -> str:
    parts = [p.strip() for p in keys.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ToolError("say which key, e.g. 'enter', 'ctrl+l', 'alt+tab'")
    return "+".join(KEY_NAMES.get(p.lower(), p) for p in parts)


@mcp.tool()
def screenshot() -> Image:
    """A picture of the whole screen right now (PNG). Use it before clicking, and after an action to see what happened."""
    _ensure_desktop()
    if not shutil.which("scrot"):
        raise ToolError("scrot is not installed in this machine")
    out = Path("/tmp/desktop-shot.png")
    r = subprocess.run(["scrot", "--overwrite", str(out)], env=_env(), capture_output=True, text=True, timeout=20)
    if r.returncode != 0 or not out.exists():
        raise ToolError(f"could not take a screenshot: {r.stderr.strip()[:200]}")
    return Image(data=out.read_bytes(), format="png")


@mcp.tool()
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at pixel (x, y) of the screenshot. button: left, right or middle; double=true for a double click."""
    if button not in BUTTONS:
        raise ToolError("button is left, right or middle")
    _act()
    _xdotool("mousemove", "--sync", str(int(x)), str(int(y)))
    _xdotool("click", *(["--repeat", "2", "--delay", "80"] if double else []), BUTTONS[button])
    return f"clicked {button}{' twice' if double else ''} at {x},{y}"


@mcp.tool()
def type_text(text: str) -> str:
    """Type text where the keyboard focus is (click the field first). Never type a password: ask the user to do that."""
    if len(text) > 4000:
        raise ToolError("type at most 4000 characters at a time")
    _act()
    _xdotool("type", "--delay", "12", "--", text)
    return f"typed {len(text)} characters"


@mcp.tool()
def key(keys: str) -> str:
    """Press a key or a combination: 'enter', 'esc', 'tab', 'ctrl+l', 'ctrl+shift+t', 'alt+tab', 'pagedown'."""
    _act()
    combo = _key_combo(keys)
    _xdotool("key", "--clearmodifiers", combo)
    return f"pressed {combo}"


@mcp.tool()
def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> str:
    """Scroll at pixel (x, y): direction up, down, left or right; amount is wheel steps (1-20)."""
    buttons = {"up": "4", "down": "5", "left": "6", "right": "7"}
    if direction not in buttons:
        raise ToolError("direction is up, down, left or right")
    _act()
    _xdotool("mousemove", "--sync", str(int(x)), str(int(y)))
    _xdotool("click", "--repeat", str(max(1, min(int(amount), 20))), "--delay", "40", buttons[direction])
    return f"scrolled {direction} {amount} at {x},{y}"


@mcp.tool()
def wait(seconds: float = 1.0) -> str:
    """Wait for the screen to settle (a page loading, an app opening), up to 10 seconds."""
    time.sleep(max(0.0, min(float(seconds), 10.0)))
    return f"waited {seconds} s"


if __name__ == "__main__":
    mcp.run()          # stdio, started by Claude Code (claude mcp add desktop -- python /opt/claude/desktop_mcp.py)
