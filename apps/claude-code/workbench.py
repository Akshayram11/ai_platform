"""The workbench behind dapp_proxy: one page with the terminal (ttyd) on the left and a real browser on the right.

The browser is shared: you see and use it through noVNC, and Claude drives the same Chromium through Playwright MCP
(over the Chrome DevTools Protocol). It costs memory, so it runs only on demand:
  - it starts when you press "Start browser" (or open its view), or when Claude connects to it (first CDP connection);
  - it stops after IDLE_MINUTES with nobody watching it and Claude not using it.

Ports (all on 127.0.0.1 except the page itself, which dapp_proxy reaches):
  7680  this server            7681  ttyd               9222  CDP for Claude (this server, starts the browser)
  9223  Chromium's own CDP     5900  x11vnc (screen)    display :99 Xvfb
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, WSMsgType, web

log = logging.getLogger("workbench")
BASE = "/" + os.environ.get("ROOT_PATH", "").strip("/")
BASE = "" if BASE == "/" else BASE
HOME = Path(os.environ.get("HOME", "/tmp/claude-home"))
TTYD = "http://127.0.0.1:7681"
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
IDLE_MINUTES = float(os.environ.get("BROWSER_IDLE_MINUTES", "15"))
SCREEN = os.environ.get("BROWSER_SCREEN", "1366x860")
DISPLAY = ":99"
CDP_PUBLIC, CDP_CHROME, VNC_PORT = 9222, 9223, 5900
PAGE = (Path(__file__).parent / "workbench.html").read_text()
# Claude's desktop tool (desktop_mcp.py) touches ACTIVE on every action; the page's "Take over" creates PAUSED, which makes
# that tool refuse to move the mouse or type until the user hands the screen back.
DESKTOP_ACTIVE, DESKTOP_PAUSED = Path("/tmp/desktop-claude-active"), Path("/tmp/desktop-paused")
# the machine's own servers: workbench, ttyd, the sign-in proxy, CDP (ours and Chromium's), VNC, X display :99, and the
# metrics exporter of dapp_proxy (dapp_metrics, read by DataOS)
INTERNAL_PORTS = {7680, 7681, 8080, 8081, 9222, 9223, 5900, 6000 + 99, 9102}
for _var in ("PORT", "DAPP_METRICS_PORT"):
    if os.environ.get(_var, "").isdigit():
        INTERNAL_PORTS.add(int(os.environ[_var]))


class Browser:
    """Xvfb + Chromium + x11vnc, started together on demand and stopped when idle."""

    def __init__(self) -> None:
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.lock = asyncio.Lock()
        self.viewers = 0          # open noVNC connections
        self.cdp_clients = 0      # open CDP connections (Claude)
        self.last_used = 0.0
        self.started_at = 0.0
        self.last_error = ""

    @property
    def running(self) -> bool:
        return bool(self.procs) and all(p.returncode is None for p in self.procs.values())

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def idle_for(self) -> float:
        if self.viewers or self.cdp_clients:
            return 0.0
        return time.monotonic() - self.last_used

    def status(self) -> dict:
        return {"running": self.running, "viewers": self.viewers, "claude_connected": self.cdp_clients > 0,
                "idle_minutes": round(self.idle_for() / 60, 1) if self.running else None,
                "stops_after_idle_minutes": IDLE_MINUTES, "error": self.last_error or None,
                "claude_on_screen": _recent(DESKTOP_ACTIVE, 10), "taken_over": DESKTOP_PAUSED.exists()}

    async def _spawn(self, name: str, *cmd: str) -> None:
        env = {**os.environ, "DISPLAY": DISPLAY}
        self.procs[name] = await asyncio.create_subprocess_exec(*cmd, env=env, stdout=asyncio.subprocess.DEVNULL,
                                                                stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    async def ensure(self) -> bool:
        async with self.lock:
            self.touch()
            if self.running:
                return True
            await self._stop_locked()
            self.last_error = ""
            chrome = shutil.which("chromium") or shutil.which("chromium-browser") or "chromium"
            profile = HOME / ".browser"
            profile.mkdir(parents=True, exist_ok=True)
            for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):   # left by a pod that was stopped hard
                with contextlib.suppress(OSError):
                    (profile / lock).unlink()
            w, h = SCREEN.split("x")
            try:
                with contextlib.suppress(FileNotFoundError):
                    Path("/tmp/.X99-lock").unlink()
                await self._spawn("xvfb", "Xvfb", DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp")
                await asyncio.sleep(0.6)
                if shutil.which("openbox"):              # a desktop: windows can be moved, and more apps opened
                    await self._spawn("openbox", "openbox")
                await self._spawn("chromium", chrome, "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                                  "--no-default-browser-check", "--disable-gpu", "--password-store=basic",
                                  f"--user-data-dir={profile}", f"--window-size={w},{h}", "--window-position=0,0",
                                  "--start-maximized", "--remote-debugging-address=127.0.0.1",
                                  f"--remote-debugging-port={CDP_CHROME}", "about:blank")
                await self._spawn("x11vnc", "x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT), "-localhost",
                                  "-nopw", "-forever", "-shared", "-quiet", "-noxdamage")
                for _ in range(60):                      # Chromium's CDP answers when it is ready
                    if await _port_open(CDP_CHROME) and await _port_open(VNC_PORT):
                        self.started_at = time.monotonic()
                        log.info("browser started")
                        return True
                    if not self.running:
                        break
                    await asyncio.sleep(0.25)
                self.last_error = "the desktop did not start (out of memory?)"
            except FileNotFoundError as e:
                self.last_error = f"missing program: {e.filename}"
            await self._stop_locked()
            return False

    async def stop(self) -> None:
        async with self.lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        keep = set()
        for name in ("x11vnc", "chromium", "openbox", "xvfb"):
            p = self.procs.pop(name, None)
            if p and p.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(p.pid, signal.SIGTERM)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(p.wait(), 5)
                if p.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(p.pid, signal.SIGKILL)
        self.procs.clear()
        await asyncio.sleep(0.5)
        reap_orphans(keep)

    async def reaper(self) -> None:
        while True:
            await asyncio.sleep(30)
            reap_orphans({p.pid for p in self.procs.values()})
            if self.procs and not self.running:          # something crashed: clean up, start fresh next time
                self.last_error = "the browser stopped by itself (out of memory?); it starts again when needed"
                await self.stop()
            elif self.running and self.idle_for() > IDLE_MINUTES * 60:
                log.info("browser idle for %.0f min: stopping it", IDLE_MINUTES)
                await self.stop()


def become_subreaper() -> None:
    """Chromium's helpers outlive it when it stops; let them become our children (not PID 1's, which never reaps them)."""
    with contextlib.suppress(Exception):
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(36, 1, 0, 0, 0)       # PR_SET_CHILD_SUBREAPER


def reap_orphans(keep: set[int]) -> int:
    """Collect zombie children that aren't the processes we track (those asyncio reaps itself)."""
    me, n = os.getpid(), 0
    if not Path("/proc").is_dir():                       # not Linux (a laptop running the tests)
        return 0
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) in keep:
            continue
        try:
            fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] == "Z" and int(fields[1]) == me:
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(int(d.name), os.WNOHANG)
                n += 1
    return n


def _recent(path: Path, seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime < seconds
    except OSError:
        return False


def own_socket_inodes(proc: Path = Path("/proc")) -> set[str] | None:
    """Sockets held by the processes this container can see (ours). DataOS's sidecars share the pod's network, so their
    ports show up in /proc/net/tcp too, but their processes don't show up here. None when there is no /proc (not Linux)."""
    if not proc.is_dir():
        return None
    inodes: set[str] = set()
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            fds = list((d / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    return inodes


def listening_ports(tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6"),
                    inodes: set[str] | None | bool = True) -> list[int]:
    """Ports an app in this machine listens on (an app being built, a dev server): not the machine's own servers, and not
    DataOS's sidecars (their sockets aren't held by our processes). inodes=True looks them up; None skips that check."""
    own = own_socket_inodes() if inodes is True else inodes
    found: set[int] = set()
    for table in tables:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) > 9 and parts[3] == "0A":                     # TCP_LISTEN
                port = int(parts[1].rsplit(":", 1)[1], 16)
                if 1024 <= port < 32768 and port not in INTERNAL_PORTS and (own is None or parts[9] in own):
                    found.add(port)
    return sorted(found)


async def _port_open(port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.5)
        w.close()
        return True
    except (TimeoutError, OSError):
        return False


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, on_data) -> None:
    try:
        while data := await reader.read(65536):
            on_data()
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def cdp_server(browser: Browser):
    """CDP for Claude on 9222: the first connection starts the browser; bytes go to Chromium's own port untouched.
    Chromium builds its webSocketDebuggerUrl from the Host header, so Playwright's follow-up connection comes back here."""
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not await browser.ensure():
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", CDP_CHROME)
        except OSError:
            writer.close()
            return
        browser.cdp_clients += 1
        try:
            await asyncio.gather(_pipe(reader, up_w, browser.touch), _pipe(up_r, writer, browser.touch))
        finally:
            browser.cdp_clients -= 1
            browser.touch()
    return handle


def make_app(browser: Browser) -> web.Application:
    app = web.Application()
    routes = web.RouteTableDef()

    @routes.get(BASE + "/")
    @routes.get(BASE or "/")
    async def page(request: web.Request) -> web.Response:
        if BASE and request.path == BASE:
            raise web.HTTPFound(BASE + "/")
        return web.Response(text=PAGE.replace("__BASE__", BASE), content_type="text/html")

    @routes.get(BASE + "/browser/status")
    async def status(request: web.Request) -> web.Response:
        return web.json_response(browser.status())

    @routes.post(BASE + "/browser/start")
    async def start(request: web.Request) -> web.Response:
        ok = await browser.ensure()
        return web.json_response(browser.status(), status=200 if ok else 503)

    @routes.post(BASE + "/browser/stop")
    async def stop(request: web.Request) -> web.Response:
        await browser.stop()
        return web.json_response(browser.status())

    @routes.get(BASE + "/browser/ws")
    async def vnc(request: web.Request) -> web.StreamResponse:
        """noVNC's websocket, bridged to x11vnc. Opening the view starts the browser."""
        if not await browser.ensure():
            raise web.HTTPServiceUnavailable(text=browser.last_error or "the browser could not start")
        ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0)
        await ws.prepare(request)
        try:
            r, w = await asyncio.open_connection("127.0.0.1", VNC_PORT)
        except OSError:
            await ws.close()
            return ws
        browser.viewers += 1

        async def up() -> None:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    browser.touch()
                    w.write(msg.data)
                    await w.drain()
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
            w.close()

        async def down() -> None:
            while data := await r.read(65536):
                await ws.send_bytes(data)
            await ws.close()

        try:
            await asyncio.gather(up(), down(), return_exceptions=True)
        finally:
            browser.viewers -= 1
            browser.touch()
        return ws

    async def novnc(request: web.Request) -> web.StreamResponse:
        rel = request.match_info["path"] or "vnc.html"
        target = (NOVNC_DIR / rel).resolve()
        if not str(target).startswith(str(NOVNC_DIR.resolve())) or not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def forward(request: web.Request, url: str) -> web.StreamResponse:
        """Pass a request (websockets included) to a server inside this machine, and its answer back unchanged."""
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection", "upgrade",
                                                                                  "sec-websocket-key", "sec-websocket-version",
                                                                                  "sec-websocket-extensions", "content-length")}
        session: ClientSession = request.app["http"]
        if request.headers.get("upgrade", "").lower() == "websocket":
            protos = tuple(p.strip() for p in request.headers.get("sec-websocket-protocol", "").split(",") if p.strip())
            headers.pop("Sec-WebSocket-Protocol", None)
            headers.pop("sec-websocket-protocol", None)
            ws = web.WebSocketResponse(protocols=protos, max_msg_size=0)
            await ws.prepare(request)
            async with session.ws_connect(url, protocols=protos, headers=headers, max_msg_size=0) as up:
                async def a() -> None:
                    async for m in ws:
                        if m.type == WSMsgType.BINARY:
                            await up.send_bytes(m.data)
                        elif m.type == WSMsgType.TEXT:
                            await up.send_str(m.data)
                        else:
                            break
                    await up.close()

                async def b() -> None:
                    async for m in up:
                        if m.type == WSMsgType.BINARY:
                            await ws.send_bytes(m.data)
                        elif m.type == WSMsgType.TEXT:
                            await ws.send_str(m.data)
                        else:
                            break
                    await ws.close()
                await asyncio.gather(a(), b(), return_exceptions=True)
            return ws
        body = await request.read()
        async with session.request(request.method, url, headers=headers, data=body or None, allow_redirects=False) as r:
            out = web.Response(status=r.status, body=await r.read())
            # the body is passed on still compressed (auto_decompress=False), so Content-Encoding must go with it: without it
            # the browser gets ttyd's gzipped page as plain HTML and the terminal stays black
            for k, v in r.headers.items():
                if k.lower() not in ("content-length", "transfer-encoding", "connection"):
                    out.headers[k] = v
            return out

    async def terminal(request: web.Request) -> web.StreamResponse:
        """Everything under /term goes to ttyd as it is (ttyd runs with --base-path <root>/term), websockets included."""
        return await forward(request, TTYD + str(request.rel_url))

    async def port(request: web.Request) -> web.StreamResponse:
        """Preview: <root>/port/3000/x goes to 127.0.0.1:3000/x, for an app being built here. Only behind the DataOS
        sign-in that lets the owner alone in (dapp_proxy), and never to the machine's own servers."""
        n = int(request.match_info["port"])
        if not 1024 <= n < 32768 or n in INTERNAL_PORTS:
            raise web.HTTPForbidden(text=f"port {n} can't be previewed")
        if Path("/proc").is_dir() and n not in listening_ports():     # only apps of this machine, not DataOS's sidecars
            return web.Response(status=502, text=f"Nothing in this machine is listening on port {n} yet.")
        tail = request.match_info["tail"]
        if not tail:                             # /port/3000 -> /port/3000/, so the app's relative links work
            raise web.HTTPFound(f"{BASE}/port/{n}/")
        if not tail.startswith("/"):
            raise web.HTTPNotFound()
        try:
            return await forward(request, f"http://127.0.0.1:{n}{tail}" + (f"?{request.query_string}" if request.query_string else ""))
        except (OSError, ClientError):
            return web.Response(status=502, text=f"Nothing is answering on port {n} in this machine yet.")

    @routes.get(BASE + "/ports")
    async def ports(request: web.Request) -> web.Response:
        return web.json_response({"ports": [{"port": n, "url": f"{BASE}/port/{n}/"} for n in listening_ports()]})

    @routes.post(BASE + "/desktop/takeover")
    async def takeover(request: web.Request) -> web.Response:
        DESKTOP_PAUSED.touch()
        return web.json_response(browser.status())

    @routes.post(BASE + "/desktop/handback")
    async def handback(request: web.Request) -> web.Response:
        with contextlib.suppress(FileNotFoundError):
            DESKTOP_PAUSED.unlink()
        return web.json_response(browser.status())

    app.add_routes(routes)
    app.router.add_get(BASE + "/browser/vnc/{path:.*}", novnc)
    app.router.add_route("*", BASE + "/term{tail:.*}", terminal)
    app.router.add_route("*", BASE + r"/port/{port:\d+}{tail:.*}", port)

    async def on_start(app: web.Application) -> None:
        app["http"] = ClientSession(auto_decompress=False)
        app["reaper"] = asyncio.create_task(browser.reaper())
        app["cdp"] = await asyncio.start_server(cdp_server(browser), "127.0.0.1", CDP_PUBLIC)

    async def on_stop(app: web.Application) -> None:
        app["reaper"].cancel()
        app["cdp"].close()
        await browser.stop()
        await app["http"].close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[workbench] %(message)s")
    become_subreaper()
    web.run_app(make_app(Browser()), host="127.0.0.1", port=int(os.environ.get("WORKBENCH_PORT", "7680")),
                access_log=None, print=None)


if __name__ == "__main__":
    main()
