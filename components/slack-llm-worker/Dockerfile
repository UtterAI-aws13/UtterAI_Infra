FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY slack_worker ./slack_worker
USER 65534:65534
STOPSIGNAL SIGTERM
CMD ["python", "-m", "slack_worker.runtime"]
