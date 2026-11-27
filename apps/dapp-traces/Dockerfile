FROM python:3.12-slim
RUN pip install --no-cache-dir mlflow==3.18.0
# DataOS runs the container as its own user, not one from this image: keep the code world-readable and HOME writable.
WORKDIR /app
COPY --chmod=0644 server.py /app/server.py
ENV HOME=/tmp PORT=8080 MLFLOW_TRACKING_URI=sqlite:////data/mlflow.db MLFLOW_DISABLE_TELEMETRY=true MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false
USER 10001
EXPOSE 8080
CMD ["python", "/app/server.py"]
