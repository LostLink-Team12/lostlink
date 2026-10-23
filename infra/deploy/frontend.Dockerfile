FROM node:22.22.0-alpine AS build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM nginxinc/nginx-unprivileged:1.29.2-alpine
COPY --from=build /frontend/dist/ /usr/share/nginx/html/
COPY infra/deploy/gateway.conf /etc/nginx/conf.d/default.conf
USER 1000
EXPOSE 8080
