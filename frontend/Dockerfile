# syntax=docker/dockerfile:1
#
# SOJOURNER frontend image.
#
# The Vite build runs in a builder stage; the runtime stage is nginx serving static files and
# proxying /api and /ws to the backend. Same-origin serving means the browser sees one
# origin, so there is no CORS configuration and the API base URL stays a relative path.

# ---------------------------------------------------------------------------------------
# Stage 1 — build
# ---------------------------------------------------------------------------------------
FROM node:22-slim AS builder

WORKDIR /build

# Dependencies are copied alone so that a source-only change reuses the cached install layer.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------------------
# Stage 2 — serve
# ---------------------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY --from=builder /build/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]