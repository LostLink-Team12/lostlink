# service-template

Golden-path template. To create a service:

1. Copy this folder to `services/<name>`.
2. Set `SERVICE_NAME` and add your routes in `app/main.py`.
3. Keep `/health`, `/ready`, the Dockerfile and at least one test. CI builds and tests every folder under `services/`.
