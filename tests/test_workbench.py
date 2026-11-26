"""Claude Code's machine (apps/claude-code): the workbench page server and the desktop tool Claude uses."""
import asyncio
import gzip
import importlib
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp import web

APP = Path(__file__).resolve().parents[1] / "apps" / "claude-code"
sys.path.insert(0, str(APP))


@pytest.fixture
def wb(monkeypatch, tmp_path):
    monkeypatch.setenv("ROOT_PATH", "/claude-code")
    monkeypatch.setenv("NOVNC_DIR", str(tmp_path))
    sys.modules.pop("workbench", None)
    mod = importlib.import_module("workbench")
    monkeypatch.setattr(mod, "DESKTOP_ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "DESKTOP_PAUSED", tmp_path / "paused")
    return mod


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


def test_listening_ports_skip_the_machines_own(wb, tmp_path):
    table = tmp_path / "tcp"
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = [("0100007F:0BB8", "0A"),      # 127.0.0.1:3000 listening: an app being built
            ("00000000:1F90", "0A"),      # :8080, the sign-in proxy
            ("0100007F:1E00", "0A"),      # :7680, the workbench
            ("0100007F:2328", "01"),      # :9000 established, not listening
            ("00000000:0016", "0A"),      # :22, below 1024
            ("00000000:1F41", "0A")]      # :8001
    table.write_text(head + "".join(f"   0: {a} 00000000:0000 {st} 00000000:00000000 00:00000000 00000000 1000 0 {100 + i}\n"
                                    for i, (a, st) in enumerate(rows)))
    assert wb.listening_ports((str(table), str(tmp_path / "missing")), inodes=None) == [3000, 8001]
    # :8001 here stands for a DataOS sidecar's port: in the pod's network, but its socket isn't held by our processes
    assert wb.listening_ports((str(table),), inodes={"100"}) == [3000]


def test_own_socket_inodes_reads_the_processes_we_can_see(wb, tmp_path):
    fd = tmp_path / "42" / "fd"
    fd.mkdir(parents=True)
    (fd / "3").symlink_to("socket:[777]")
    (fd / "4").symlink_to("/dev/null")
    (tmp_path / "self").mkdir()
    assert wb.own_socket_inodes(tmp_path) == {"777"}
    assert wb.own_socket_inodes(tmp_path / "nope") is None


def test_port_preview_terminal_gzip_and_take_over(wb):
    page = b"<!doctype html><title>app</title>"

    async def app_page(request):                 # an app Claude runs, on :18300
        return web.Response(body=page, content_type="text/html", headers={"x-path": request.path_qs})

    async def ttyd_page(request):                # ttyd sends its page gzipped
        return web.Response(body=gzip.compress(b"<!DOCTYPE html>ttyd"), headers={"Content-Encoding": "gzip",
                                                                                 "Content-Type": "text/html"})

    async def run():
        a = web.Application()
        a.router.add_get("/{tail:.*}", app_page)
        app_runner = await serve(a, 18300)
        t = web.Application()
        t.router.add_get("/claude-code/term/", ttyd_page)
        monkey_ttyd = wb.TTYD
        wb.TTYD = "http://127.0.0.1:18301"
        ttyd_runner = await serve(t, 18301)
        wb_runner = await serve(wb.make_app(wb.Browser()), 18302)
        try:
            async with httpx.AsyncClient(base_url="http://127.0.0.1:18302/claude-code") as c:
                r = await c.get("/port/18300/x/y?z=1")
                assert r.status_code == 200 and r.content == page and r.headers["x-path"] == "/x/y?z=1"
                assert (await c.get("/port/18300", follow_redirects=False)).headers["location"] == "/claude-code/port/18300/"
                assert (await c.get("/port/8080/")).status_code == 403              # the machine's own servers: never
                assert (await c.get("/port/7681/")).status_code == 403
                assert (await c.get("/port/9102/")).status_code == 403              # dapp_metrics, read by DataOS
                assert (await c.get("/port/80/")).status_code == 403
                gone = await c.get("/port/18399/")
                assert gone.status_code == 502 and "Nothing is answering" in gone.text
                term = await c.get("/term/", headers={"accept-encoding": "gzip"})
                assert term.headers["content-encoding"] == "gzip" and term.text == "<!DOCTYPE html>ttyd"
                st = (await c.post("/desktop/takeover")).json()
                assert st["taken_over"] is True and wb.DESKTOP_PAUSED.exists()
                assert (await c.get("/browser/status")).json()["taken_over"] is True
                assert (await c.post("/desktop/handback")).json()["taken_over"] is False
                wb.DESKTOP_ACTIVE.touch()
                assert (await c.get("/browser/status")).json()["claude_on_screen"] is True
                assert (await c.get("/ports")).status_code == 200
        finally:
            wb.TTYD = monkey_ttyd
            for r in (wb_runner, ttyd_runner, app_runner):
                await r.cleanup()

    asyncio.run(run())


@pytest.fixture
def desk(monkeypatch, tmp_path):
    sys.modules.pop("desktop_mcp", None)
    mod = importlib.import_module("desktop_mcp")
    monkeypatch.setattr(mod, "ACTIVE", tmp_path / "active")
    monkeypatch.setattr(mod, "PAUSED", tmp_path / "paused")
    monkeypatch.setattr(mod, "_ensure_desktop", lambda: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    class Done:
        returncode, stderr, stdout = 0, "", ""

    def run(cmd, **kw):
        calls.append(cmd)
        assert kw["env"]["DISPLAY"] == ":99"
        return Done()

    monkeypatch.setattr(mod.subprocess, "run", run)
    return mod, calls


def test_desktop_tool_drives_the_screen_and_respects_take_over(desk):
    mod, calls = desk
    assert mod.click(10, 20) == "clicked left at 10,20"
    assert calls[-2:] == [["xdotool", "mousemove", "--sync", "10", "20"], ["xdotool", "click", "1"]]
    mod.click(5, 6, button="right", double=True)
    assert calls[-1] == ["xdotool", "click", "--repeat", "2", "--delay", "80", "3"]
    assert mod.key("ctrl+l") == "pressed ctrl+l" and calls[-1] == ["xdotool", "key", "--clearmodifiers", "ctrl+l"]
    assert mod.key("Enter") == "pressed Return"
    mod.type_text("hello world")
    assert calls[-1] == ["xdotool", "type", "--delay", "12", "--", "hello world"]
    mod.scroll(1, 2, "up", 50)
    assert calls[-1] == ["xdotool", "click", "--repeat", "20", "--delay", "40", "4"]
    assert mod.ACTIVE.exists()                                   # the page shows "Claude is using the mouse and keyboard"

    mod.PAUSED.touch()                                           # the user pressed Take over
    n = len(calls)
    for act in (lambda: mod.click(1, 1), lambda: mod.type_text("x"), lambda: mod.key("enter"), lambda: mod.scroll(1, 1)):
        with pytest.raises(mod.ToolError, match="taken over"):
            act()
    assert len(calls) == n                                       # nothing reached the screen
    with pytest.raises(mod.ToolError, match="left, right or middle"):
        mod.click(1, 1, button="side")
