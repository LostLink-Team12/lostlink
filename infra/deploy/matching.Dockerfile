# Reuse a published immutable ML dependency layer, then install current code.
# BASE_IMAGE must be an ECR digest from the successful C2 build (INSTALL_ML=true).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY libs /tmp/libs
RUN pip install --no-cache-dir --no-deps /tmp/libs && rm -rf /tmp/libs && pip check
COPY services/matching-service/matching_app /workspace/matching_app
COPY services/matching-service/migrations /workspace/migrations
COPY services/matching-service/alembic.ini /workspace/alembic.ini
USER appuser
