# Build from the repo root so the shared login module is in the context:
#   docker build -f examples/chatbot/Dockerfile -t <registry>/dapp-chatbot:0.1.0 .
FROM python:3.12-slim
WORKDIR /app
COPY examples/chatbot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY examples/chatbot/app.py examples/chatbot/index.html src/dapp_login.py src/dapp_metrics.py ./
RUN useradd --uid 10001 app
USER 10001
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
