# docker build -f examples/mcp-tool/Dockerfile -t docker.io/tmdcio/dapp-demo-tools:0.1.0 .
FROM python:3.12-slim
WORKDIR /app
COPY examples/mcp-tool/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY examples/mcp-tool/server.py ./
RUN useradd --uid 10001 app
USER 10001
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
