# Image for a gateway MCP tool app (apps/immortal-obj, apps/dapp-monitor): its code and `mcp` baked in, so the pod
# downloads nothing at start (the poliksdp01 dataplane can't reach files.pythonhosted.org).
#   docker buildx build --platform linux/amd64 -f apps/mcp-tool.Dockerfile --build-arg APP=immortal-obj \
#     -t docker.io/tmdcio/dapp-immortal-obj:0.1.0 --push .
FROM python:3.12-slim
ARG APP
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
COPY apps/${APP}/app/requirements.txt /tmp/requirements.txt
RUN uv pip install --system --no-cache -r /tmp/requirements.txt uvicorn httpx && rm /tmp/requirements.txt
COPY apps/${APP}/app/*.py /app/
RUN useradd --uid 10001 app
USER 10001
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
