FROM node:22.22.0-alpine AS build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
# Runtime matches the dev/CI linux-amd64 platform; do not float a security fix.
FROM nginxinc/nginx-unprivileged:1.30.6-alpine-slim@sha256:07783a7bd042ac2229183c0660aca15fcdf9a006bb4e37c7efd78482bc3900b9
COPY --from=build /frontend/dist/ /usr/share/nginx/html/
COPY infra/deploy/gateway.conf /etc/nginx/conf.d/default.conf
USER 1000
EXPOSE 8080
