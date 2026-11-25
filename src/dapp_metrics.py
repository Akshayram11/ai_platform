"""Self-reported container metrics for apps deployed with dapp. Standard library only, copy it into any image.

DataOS does not collect pod CPU/memory for app namespaces, but it does scrape a `metricEndpoint` that an app
exposes (a sidecar reads http://localhost:<port><path> inside the pod and remote-writes it to Prometheus).
This module serves that endpoint from the container's own cgroup, on a separate internal port that the
ingress and the sign-in layer never see:

    import dapp_metrics
    dapp_metrics.start()                 # serves /metrics on $DAPP_METRICS_PORT (default 9102)
    app = dapp_metrics.instrument(app)   # optional: request counts for an ASGI app

Or next to a server that isn't Python (llm-small runs it beside llama.cpp): `python3 dapp_metrics.py &`.

dapp sets `metricEndpoint: {port: 9102, path: /metrics}` for apps with `metrics: true`.

Series (all prefixed dapp_ so they never clash with an app's own metrics):
    dapp_container_cpu_usage_seconds_total   counter, CPU seconds used by the container
    dapp_container_memory_working_set_bytes  gauge, memory in use (cgroup, minus reclaimable page cache)
    dapp_container_cpu_limit_cores           gauge, 0 when unlimited
    dapp_container_memory_limit_bytes        gauge, 0 when unlimited
    dapp_container_uptime_seconds            gauge
    dapp_http_requests_total{code}           counter, only with instrument()
    dapp_http_request_duration_seconds_sum / _count
"""

from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")
STARTED = time.time()
_requests: dict[str, int] = {}
_duration_sum = 0.0
_duration_count = 0
_lock = threading.Lock()


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _kv(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (_read(path) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            out[parts[0]] = int(parts[1])
    return out


def cpu_seconds() -> float:
    stat = _kv(CGROUP / "cpu.stat")  # cgroup v2
    if "usage_usec" in stat:
        return stat["usage_usec"] / 1e6
    v1 = _read(CGROUP / "cpuacct" / "cpuacct.usage")  # cgroup v1, nanoseconds
    if v1 and v1.isdigit():
        return int(v1) / 1e9
    return time.process_time()  # not in a container: this process only


def memory_working_set() -> int:
    cur = _read(CGROUP / "memory.current")  # v2
    if cur and cur.isdigit():
        inactive = _kv(CGROUP / "memory.stat").get("inactive_file", 0)
        return max(int(cur) - inactive, 0)
    cur = _read(CGROUP / "memory" / "memory.usage_in_bytes")  # v1
    if cur and cur.isdigit():
        inactive = _kv(CGROUP / "memory" / "memory.stat").get("total_inactive_file", 0)
        return max(int(cur) - inactive, 0)
    statm = _read(Path("/proc/self/statm"))  # not in a container: this process's resident set
    if statm:
        return int(statm.split()[1]) * os.sysconf("SC_PAGE_SIZE")
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if rss > 10**8 else rss * 1024  # macOS reports bytes, Linux kilobytes


def cpu_limit_cores() -> float:
    raw = _read(CGROUP / "cpu.max")  # v2: "<quota|max> <period>"
    if raw:
        quota, _, period = raw.partition(" ")
        if quota.isdigit() and period.isdigit() and int(period) > 0:
            return int(quota) / int(period)
        return 0.0
    quota, period = _read(CGROUP / "cpu" / "cpu.cfs_quota_us"), _read(CGROUP / "cpu" / "cpu.cfs_period_us")
    if quota and period and quota.lstrip("-").isdigit() and int(quota) > 0 and int(period) > 0:
        return int(quota) / int(period)
    return 0.0


def memory_limit_bytes() -> int:
    for p in (CGROUP / "memory.max", CGROUP / "memory" / "memory.limit_in_bytes"):
        raw = _read(p)
        if raw and raw.isdigit() and int(raw) < 1 << 60:  # v1 reports a huge number for "unlimited"
            return int(raw)
    return 0


def _pod() -> str:
    return os.environ.get("HOSTNAME", "local").replace("\\", "").replace('"', "")


def render() -> str:
    """Every series carries pod="<pod name>", so replicas stay separate (the DataOS sidecar adds no pod label)."""
    pod = f'pod="{_pod()}"'
    lines = [
        "# TYPE dapp_container_cpu_usage_seconds_total counter",
        f"dapp_container_cpu_usage_seconds_total{{{pod}}} {cpu_seconds():.3f}",
        "# TYPE dapp_container_memory_working_set_bytes gauge",
        f"dapp_container_memory_working_set_bytes{{{pod}}} {memory_working_set()}",
        "# TYPE dapp_container_cpu_limit_cores gauge",
        f"dapp_container_cpu_limit_cores{{{pod}}} {cpu_limit_cores():.3f}",
        "# TYPE dapp_container_memory_limit_bytes gauge",
        f"dapp_container_memory_limit_bytes{{{pod}}} {memory_limit_bytes()}",
        "# TYPE dapp_container_uptime_seconds gauge",
        f"dapp_container_uptime_seconds{{{pod}}} {time.time() - STARTED:.0f}",
    ]
    with _lock:
        if _requests:
            lines.append("# TYPE dapp_http_requests_total counter")
            lines += [f'dapp_http_requests_total{{code="{c}",{pod}}} {n}' for c, n in sorted(_requests.items())]
            lines += ["# TYPE dapp_http_request_duration_seconds summary",
                      f"dapp_http_request_duration_seconds_sum{{{pod}}} {_duration_sum:.6f}",
                      f"dapp_http_request_duration_seconds_count{{{pod}}} {_duration_count}"]
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != os.environ.get("DAPP_METRICS_PATH", "/metrics"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start(port: int | None = None) -> int:
    """Serve /metrics in a background thread. Safe to call twice; returns the port."""
    port = port or int(os.environ.get("DAPP_METRICS_PORT", "9102"))
    if getattr(start, "_port", None):
        return start._port
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError:
        return 0  # port taken (e.g. a second worker process); one exporter per container is enough
    threading.Thread(target=server.serve_forever, name="dapp-metrics", daemon=True).start()
    start._port = port
    return port


def _count(status: int, seconds: float) -> None:
    global _duration_sum, _duration_count
    with _lock:
        key = f"{status // 100}xx"
        _requests[key] = _requests.get(key, 0) + 1
        _duration_sum += seconds
        _duration_count += 1


def instrument(app):
    """ASGI middleware counting requests by status class and total time."""

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        started = time.perf_counter()
        status = {"code": 500}

        async def tracking_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await app(scope, receive, tracking_send)
        finally:
            _count(status["code"], time.perf_counter() - started)

    return middleware


if __name__ == "__main__":  # standalone, next to a server that isn't Python: python3 dapp_metrics.py &
    start()
    threading.Event().wait()
