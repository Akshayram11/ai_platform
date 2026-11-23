"""demo-tools: a small MCP server (Streamable HTTP) to put behind the dapp gateway.

Nobody calls it directly. It only answers requests that carry its token (TOOL_TOKEN, from a DataOS secret), and
only the gateway has that token: people and apps reach it at <gateway>/mcp/<tenant>/demo-tools, where the gateway
checks their DataOS identity, the tool's allow list, and records the call. The gateway tells it who is calling
in x-dapp-user.
"""

from __future__ import annotations

import ast
import hmac
import operator
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

TOKEN = os.environ.get("TOOL_TOKEN", "")
mcp = MCPServer("demo-tools", instructions="Small utilities: time zones, arithmetic, text stats.")

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
       ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ToolError("exponent too large")
        return OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    raise ToolError("only numbers and + - * / // % ** ( ) are allowed")


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression exactly, e.g. "(1200 * 0.18) + 35"."""
    if len(expression) > 200:
        raise ToolError("expression too long")
    try:
        return str(_eval(ast.parse(expression, mode="eval")))
    except (SyntaxError, ZeroDivisionError, OverflowError) as e:
        raise ToolError(f"can't evaluate {expression!r}: {e}") from e


@mcp.tool()
def time_in(timezone: str = "UTC") -> str:
    """Current date and time in an IANA time zone, e.g. Asia/Kolkata or America/New_York."""
    try:
        return datetime.now(UTC).astimezone(ZoneInfo(timezone)).isoformat(timespec="seconds")
    except ZoneInfoNotFoundError as e:
        raise ToolError(f"unknown time zone {timezone!r}") from e


@mcp.tool()
def text_stats(text: str) -> dict:
    """Characters, words, lines and a rough token estimate for a piece of text."""
    return {"characters": len(text), "words": len(text.split()), "lines": text.count("\n") + 1 if text else 0,
            "approx_tokens": round(len(text) / 4)}


def build():
    hosts = [h.strip() for h in os.environ.get("TOOL_ALLOWED_HOSTS", "demo-tools:8080,demo-tools,localhost:8080")
             .split(",") if h.strip()]
    inner = mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0",
                                    transport_security=TransportSecuritySettings(allowed_hosts=hosts))

    async def app(scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not TOKEN or not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "only the dapp gateway may call this"}'})
                return
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await inner(scope, receive, send)

    return app


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("TOOL_TOKEN is not set")
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
