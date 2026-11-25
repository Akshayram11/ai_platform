# llama.cpp's OpenAI-compatible server, pinned (build 12679), re-published so DataOS pulls it from Docker Hub.
# Next to it runs dapp_metrics (port 9102): DataOS doesn't collect pod CPU/memory, so the dashboard gets them from it.
#   docker buildx build --platform linux/amd64 -f apps/llm-small/Dockerfile -t docker.io/tmdcio/llm-small:b12679-m1 --push .
FROM ghcr.io/ggml-org/llama.cpp:server@sha256:9222db8bf58b508a36df219e16c9c9d6e2e15c6e0b80df03896bba09a14b8ccf
ENV LLAMA_CACHE=/models HF_HOME=/models/hf
COPY src/dapp_metrics.py /opt/dapp/dapp_metrics.py
COPY apps/llm-small/start.sh /opt/dapp/start.sh
ENTRYPOINT ["/opt/dapp/start.sh"]
