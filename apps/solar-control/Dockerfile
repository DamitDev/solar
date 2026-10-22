# Stage 1: JS workspace dependencies (uses the workspace root lockfile)
FROM node:24-alpine AS deps
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.11.0 --activate
COPY pnpm-lock.yaml package.json pnpm-workspace.yaml ./
COPY apps/solar-status/package.json apps/solar-status/
# No --filter: unfiltered installs correctly honor onlyBuiltDependencies
# (esbuild postinstall) on fresh stores.
RUN pnpm install --frozen-lockfile

# Stage 2: build the embedded solar-status SPA
FROM node:24-alpine AS builder
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.11.0 --activate
COPY pnpm-lock.yaml package.json pnpm-workspace.yaml ./
COPY --from=deps /app/node_modules node_modules
COPY --from=deps /app/apps/solar-status/node_modules apps/solar-status/node_modules
COPY apps/solar-status/ apps/solar-status/

RUN pnpm --filter solar-status build

# Stage 3: runtime — solar-control with the status SPA baked in
FROM python:3.12-slim

ARG APP_VERSION=0.0.0-dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY apps/solar-control/requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY apps/solar-control/ ./

RUN sed -i "s/^version = .*/version = \"${APP_VERSION}\"/" pyproject.toml && \
    pip install --no-cache-dir -e .

# Baked-in status SPA at a fixed path; STATUS_DIST_DIR switches
# solar-control's frontend mount to the embedded serving mode.
COPY --from=builder /app/apps/solar-status/dist /app/status-ui
ENV STATUS_DIST_DIR=/app/status-ui

RUN useradd --create-home --uid 1000 appuser && \
    mkdir -p /app/data && \
    chmod +x entrypoint.sh && \
    chown -R appuser:appuser /app

USER appuser

ENV HOST=0.0.0.0
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:sio_asgi_app --host $HOST --port $PORT"]
