FROM python:3.12-slim
WORKDIR /workspace
RUN useradd --create-home appuser
COPY requirements.lock .
COPY libs ./libs
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps ./libs
COPY services/notification-service/notification_app ./notification_app
COPY services/notification-service/migrations ./migrations
COPY services/notification-service/alembic.ini .
USER appuser
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
CMD ["uvicorn", "notification_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
