# One image for the gateway and the dashboard; pick with the command.
#   docker build -t <registry>/dataos-apps:0.1.12 .
#   command: [dapp-gateway]   or   [dapp-dashboard]
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache ".[gateway,dashboard]"

FROM python:3.12-slim
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/dapp* /usr/local/bin/
RUN useradd --uid 10001 --create-home app
USER 10001
WORKDIR /home/app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["dapp-dashboard"]
