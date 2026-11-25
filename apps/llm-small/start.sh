#!/bin/sh
# Say what the model Volume holds: files here at start mean no download (the Volume kept them across restarts).
if [ -d "${LLAMA_CACHE:-/models}" ]; then
  echo "[llm-small] model cache ${LLAMA_CACHE:-/models}: $(du -sh "${LLAMA_CACHE:-/models}" 2>/dev/null | cut -f1)," \
       "$(find "${LLAMA_CACHE:-/models}" -name '*.gguf' 2>/dev/null | wc -l | tr -d ' ') model file(s) already there"
fi
# CPU/memory reporter in the background (it reads this container's cgroup), then llama.cpp as the main process.
python3 /opt/dapp/dapp_metrics.py &
exec /app/llama-server "$@"
