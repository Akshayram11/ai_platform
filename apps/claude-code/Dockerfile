# Claude Code in the browser, on DataOS: a web terminal (ttyd) behind DataOS sign-in (dapp_proxy), owner only.
#   docker buildx build --platform linux/amd64 -f apps/claude-code/Dockerfile -t docker.io/tmdcio/dapp-claude-code:0.1.7 --push .
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim
ARG CLAUDE_CODE_VERSION=2.1.330
ARG TTYD_VERSION=1.7.7
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates less procps tmux ncurses-term \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && curl -fsSL -o /usr/local/bin/ttyd \
       "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
    && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - && chmod 755 /usr/local/bin/ttyd
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
# dapp itself, so Claude deploys with the same CLI people use (dapp_proxy/dapp_login/dapp_metrics come with it)
COPY pyproject.toml README.md /src/
COPY src /src/src
RUN uv pip install --system --no-cache "/src[gateway]" && rm -rf /src
COPY apps/claude-code/start.sh apps/claude-code/session.sh apps/claude-code/claude-loop.sh apps/claude-code/tmux.conf \
     apps/claude-code/CLAUDE.md apps/claude-code/workspace-sync.py /opt/claude/
RUN chmod 755 /opt/claude/*.sh && useradd --uid 10001 --create-home app
USER 10001
ENV PORT=8080 PYTHONUNBUFFERED=1 DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
EXPOSE 8080
CMD ["/opt/claude/start.sh"]
