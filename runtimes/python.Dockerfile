# dapp runtime: run apps from source, without building an image per app.
#   docker buildx build --platform linux/amd64 -f runtimes/python.Dockerfile --build-arg PY=3.12 \
#     -t docker.io/tmdcio/dapp-runtime:python-3.12-rN -t docker.io/tmdcio/dapp-runtime:python-3.12 --push .
# then set RUNTIME_REVISION = "rN" in src/dapp/spec.py (dapp deploys the pinned tag).
ARG PY=3.12
FROM python:${PY}-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
RUN useradd --uid 10001 --create-home app
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH DAPP_VENV_PYTHON=/opt/venv/bin/python \
    PYTHONUNBUFFERED=1 PORT=8080
# Common libraries are already here, so most apps start without installing anything. The venv belongs to the app
# user, so requirements.txt can add more at start.
COPY runtimes/python-base.txt /tmp/base.txt
RUN uv venv /opt/venv && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/base.txt \
    && chown -R 10001:10001 /opt/venv && rm -rf /root/.cache
COPY runtimes/dapp_run.py src/dapp_login.py src/dapp_metrics.py src/dapp_proxy.py src/dapp_objects.py /opt/dapp/
ENV PYTHONPATH=/opt/dapp
USER 10001
ENV UV_CACHE_DIR=/home/app/.cache/uv
WORKDIR /home/app
EXPOSE 8080
CMD ["python", "/opt/dapp/dapp_run.py"]
