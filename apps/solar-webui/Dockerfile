# Stage 1: Build the React application
FROM node:20-alpine AS builder

ARG APP_VERSION=0.0.0-dev

WORKDIR /app

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci

# Copy source code
COPY . .

# Patch version before build
RUN sed -i "s/\"version\": \".*\"/\"version\": \"${APP_VERSION}\"/" package.json

# Build the application
RUN npm run build

# Stage 2: Run the middleware server
FROM node:20-alpine

ARG APP_VERSION=0.0.0-dev

WORKDIR /app
ENV NODE_ENV=production \
    APP_VERSION=${APP_VERSION}

# Copy patched package files from builder and install production dependencies
COPY --from=builder /app/package.json ./
COPY package-lock.json ./
RUN npm ci --omit=dev

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
