FROM python:3.12-slim
WORKDIR /workspace
RUN useradd --create-home appuser
COPY requirements.lock .
COPY libs ./libs
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps ./libs
COPY services/matching-service/requirements-ml.lock .
ARG INSTALL_ML=false
RUN if [ "$INSTALL_ML" = "true" ]; then pip install --no-cache-dir -r requirements-ml.lock; fi
COPY services/matching-service/matching_app ./matching_app
COPY services/matching-service/migrations ./migrations
COPY services/matching-service/alembic.ini .
ENV HF_HUB_OFFLINE=1
USER appuser
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
CMD ["uvicorn", "matching_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
