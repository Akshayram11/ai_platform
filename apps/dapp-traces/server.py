"""Require the gateway credential on both MLflow's REST and OTLP endpoints."""

import base64
import os
import secrets

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse


def create_app():
    password = os.environ["DAPP_TRACE_PASSWORD"]
    if len(password) < 32:
        raise RuntimeError("DAPP_TRACE_PASSWORD must be at least 32 characters")

    # These are the same settings the MLflow CLI supplies to its pinned ASGI worker.
    from mlflow.server.constants import ARTIFACT_ROOT_ENV_VAR, BACKEND_STORE_URI_ENV_VAR

    os.environ[BACKEND_STORE_URI_ENV_VAR] = os.environ["MLFLOW_TRACKING_URI"]
    os.environ[ARTIFACT_ROOT_ENV_VAR] = os.environ.get("MLFLOW_ARTIFACT_ROOT", "/data/artifacts")

    from mlflow.server.fastapi_app import app as mlflow_app

    expected = b"Basic " + base64.b64encode(f"dapp-traces:{password}".encode())

    async def app(scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] in {"/healthz", "/dapp-traces/healthz"} and scope["method"] == "GET":
                return await PlainTextResponse("ok")(scope, receive, send)
            supplied = Headers(scope=scope).get("authorization", "").encode("latin-1")
            if not secrets.compare_digest(supplied, expected):
                return await PlainTextResponse(
                    "Authentication required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="DataOS Traces"'},
                )(scope, receive, send)
        elif scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await mlflow_app(scope, receive, send)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
