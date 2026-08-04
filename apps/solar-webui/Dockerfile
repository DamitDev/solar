# Stage 1: dependencies (uses the workspace root lockfile)
FROM node:24-alpine AS deps
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.11.0 --activate
COPY pnpm-lock.yaml package.json pnpm-workspace.yaml ./
COPY apps/solar-webui/package.json apps/solar-webui/
# No --filter: the workspace has a single JS package, and unfiltered installs
# correctly honor onlyBuiltDependencies (esbuild postinstall) on fresh stores.
RUN pnpm install --frozen-lockfile

# Stage 2: build
FROM node:24-alpine AS builder
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.11.0 --activate
COPY pnpm-lock.yaml package.json pnpm-workspace.yaml ./
COPY --from=deps /app/node_modules node_modules
COPY --from=deps /app/apps/solar-webui/node_modules apps/solar-webui/node_modules
COPY apps/solar-webui/ apps/solar-webui/

ARG APP_VERSION=0.0.0-dev
RUN sed -i "s/\"version\": \".*\"/\"version\": \"${APP_VERSION}\"/" apps/solar-webui/package.json

RUN pnpm --filter solar-webui build

# Stage 3: runtime — express middleware server
FROM node:24-alpine
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.11.0 --activate
COPY pnpm-lock.yaml package.json pnpm-workspace.yaml ./
COPY apps/solar-webui/package.json apps/solar-webui/
RUN pnpm install --frozen-lockfile --prod

ARG APP_VERSION=0.0.0-dev
ENV NODE_ENV=production \
    APP_VERSION=${APP_VERSION}

# server/index.js resolves ../dist and ../package.json via __dirname,
# so the app dir keeps its layout (dist + server + node_modules).
COPY --from=builder --chown=node:node /app/apps/solar-webui/dist apps/solar-webui/dist
COPY --from=builder --chown=node:node /app/apps/solar-webui/server apps/solar-webui/server

USER node

# Set port from build arg
ARG PORT=8080
ENV PORT=${PORT}

# Expose port
EXPOSE ${PORT}

# Start the middleware server
CMD ["node", "apps/solar-webui/server/index.js"]
