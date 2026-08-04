# Stage 1: Build the React application
FROM node:24-alpine AS builder

ARG APP_VERSION=0.0.0-dev

WORKDIR /app

# Copy package files
COPY package.json pnpm-lock.yaml ./

# Install dependencies
RUN corepack enable && pnpm install --frozen-lockfile

# Copy source code
COPY . .

# Patch version before build
RUN sed -i "s/\"version\": \".*\"/\"version\": \"${APP_VERSION}\"/" package.json

# Build the application
RUN pnpm run build

# Stage 2: Run the middleware server
FROM node:24-alpine

ARG APP_VERSION=0.0.0-dev

WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=${APP_VERSION}

# Copy patched package files from builder and install production dependencies
COPY --from=builder /app/package.json ./
COPY --from=builder /app/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --prod --frozen-lockfile

# Use the existing node user (UID 1000 in alpine)
# Copy built assets and server
COPY --from=builder --chown=node:node /app/dist ./dist
COPY --from=builder --chown=node:node /app/server ./server

# Switch to non-root user
USER node

# Set port from build arg
ARG PORT=8080
ENV PORT=${PORT}

# Expose port
EXPOSE ${PORT}

# Start the middleware server
CMD ["node", "server/index.js"]
