# Solar WebUI

A modern React dashboard for managing distributed AI model deployments through solar-host and solar-control. Supports multiple backend types including llama.cpp and HuggingFace models.

## Preview

### Real-Time Routing Visualization
Watch your API requests flow through the system in real-time with an interactive network graph.

![Live Routing Graph](preview_live_routing.png)

### Dashboard & Instance Management
Manage all your hosts and model instances from a beautiful, unified interface.

![Dashboard View](preview_dashboard.png)

## Features

- **Multi-backend support** - Manage llama.cpp, HuggingFace Causal LM, Classification, and Embedding models
- **Real-time routing visualization** - Interactive network graph showing API request flow
- **Dashboard view** - Manage all solar-hosts and model instances via Socket.IO (`/webui` namespace)
- **Pending host approval** - See and approve or reject hosts that register with solar-control before they join the pool
- **Live log streaming** - Real-time WebSocket log viewer for each instance
- **Instance management** - Start, stop, restart, create, edit, and delete instances
- **Host management** - Add, remove, and monitor solar-host connections
- **Backend-aware UI** - Visual distinction between backend types with icons and colors
- **Runtime config** - API key injected by the server into the page at runtime (no build-time env needed for Docker)
- **Nord dark theme** - Beautiful arctic-inspired color scheme
- **Modern UI** - Built with React, TypeScript, Vite, and Tailwind CSS

## Supported Backend Types

| Backend | Icon | Description |
|---------|------|-------------|
| **llama.cpp** | 🔵 CPU | GGUF models via llama-server |
| **HuggingFace Causal** | 🟢 Brain | Text generation models (AutoModelForCausalLM) |
| **HuggingFace Classification** | 🟡 Tags | Sequence classification models (AutoModelForSequenceClassification) |
| **HuggingFace Embedding** | 🟣 Binary | Embedding models using last hidden state (AutoModel) |

## Installation

```bash
# Install dependencies
npm install
```

## Configuration

The webui ships with a built-in middleware proxy with **HTTP keep-alive optimizations** for low-latency performance. Configure it using environment variables (either via a `.env` file or by exporting them in your shell):

```bash
# URL of your solar-control deployment
SOLAR_CONTROL_URL=http://localhost:8000

# Management API key (solar-control). Required for REST and Socket.IO.
SOLAR_CONTROL_API_KEY=your-solar-control-api-key

# Optional: require a login key before serving the WebUI or proxying control requests
# SOLAR_WEBUI_AUTH_KEY=your-webui-login-key

# Optional: port for the middleware server (defaults to 8080)
# PORT=8080

# Optional: enable debug logging for proxy requests
# SOLAR_WEBUI_DEBUG=true
```

- **Production (Express server):** The server reads these at startup and (1) optionally gates the WebUI and `/api/control` proxy behind `SOLAR_WEBUI_AUTH_KEY`, (2) injects `X-API-Key` and `Authorization` on **every** proxied request and on **WebSocket upgrade** requests (so Socket.IO to solar-control is authenticated), and (3) injects `window.__SOLAR_CONFIG__ = { SOLAR_CONTROL_API_KEY: "..." }` into the served `index.html` so the client can send the key in Socket.IO `auth` as well. No build-time env vars are required in Docker.
- **Development (Vite):** The dev server reads `SOLAR_CONTROL_URL`, `SOLAR_CONTROL_API_KEY`, and `SOLAR_WEBUI_AUTH_KEY` from `.env` or the shell. Use `VITE_SOLAR_CONTROL_API_KEY` only when the browser connects directly to solar-control; do not use a `VITE_` prefix for `SOLAR_WEBUI_AUTH_KEY` because that would expose the WebUI login key to the browser bundle.

### Performance and Auth

The middleware server includes:
- **HTTP Keep-Alive** - Reuses TCP connections to reduce latency (5-20ms vs 50-100ms)
- **Connection Pooling** - Maintains up to 50 concurrent connections with 10 idle connections ready
- **Optional WebUI auth** - Set `SOLAR_WEBUI_AUTH_KEY` to require a simple login before the SPA, REST proxy, or WebSocket proxy can be used. If unset, the WebUI keeps its previous open behavior.
- **WebSocket upgrade auth** - The proxy’s `headers` option does not apply to WebSocket upgrades. The server explicitly sets `X-API-Key` and `Authorization` on the upgrade request before proxying, so solar-control can authenticate the Socket.IO connection.
- **Runtime config injection** - In production, the server injects `window.__SOLAR_CONFIG__` into the HTML so the client gets the API key at runtime (works in Docker without build-time env).
- **Compression** - Gzip compression for static assets
- **ETag Disabled** - Reduces overhead for proxied requests

## Development

```bash
# Start development server
npm run dev
```

The application will be available at `http://localhost:5173`. The dev server proxies `/api/control/*` and related WebSocket routes to `SOLAR_CONTROL_URL`, injecting the API key from your environment variables. If `SOLAR_WEBUI_AUTH_KEY` is set, the dev server uses the same login cookie gate as production.

## Production Deployment

### Option 1: Docker (Recommended)

**Quick Start:**

```bash
# Export the required variables (or place them in docker-compose.env)
export SOLAR_CONTROL_URL=http://host.docker.internal:8015
export SOLAR_CONTROL_API_KEY=your-solar-control-api-key
export SOLAR_WEBUI_AUTH_KEY=your-webui-login-key  # optional
export PORT=8080  # optional

# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

**Important Docker Notes:**

- The middleware listens on `http://localhost:PORT` (default `8080`)
- Uses `host.docker.internal` to access solar-control running on the host machine
- Environment variables are read at container runtime—no rebuild is required when they change

### Option 2: Build for Production (Native)

```bash
npm run build
SOLAR_CONTROL_URL=http://localhost:8000 SOLAR_CONTROL_API_KEY=your-key SOLAR_WEBUI_AUTH_KEY=your-webui-login-key npm start
```

This builds the React assets and launches the Node middleware (`npm start` is an alias for `npm run serve`). The middleware serves both static files and all API/WebSocket requests, so you do **not** need an additional reverse proxy unless you want TLS or custom routing. Changes to environment variables take effect on the next process restart.

## Project Structure

```
src/
├── main.tsx                    # Entry point
├── App.tsx                     # Main app with routing and navigation
├── index.css                   # Global styles with Nord theme
├── api/
│   ├── client.ts               # Axios client configuration
│   └── types.ts                # TypeScript type definitions (multi-backend)
├── components/
│   ├── RoutingGraph.tsx        # Real-time routing visualization
│   ├── Dashboard.tsx           # Hosts & instances dashboard
│   ├── HostCard.tsx            # Host display card with backend summary
│   ├── InstanceCard.tsx        # Instance display card (backend-aware)
│   ├── LogViewer.tsx           # Real-time log viewer modal
│   ├── AddHostModal.tsx        # Add host modal
│   ├── AddInstanceModal.tsx    # Create instance modal (multi-backend)
│   └── EditInstanceModal.tsx   # Edit instance modal (backend-aware)
├── hooks/
│   ├── useWebSocket.ts         # WebSocket management hook
│   ├── useEventStream.ts       # Socket.IO /webui event stream (hosts, instances, gateway, pending)
│   ├── useInstances.ts         # Instance data management hook
│   ├── useHostStatus.ts        # Real-time host status updates
│   └── useRoutingEvents.ts    # Routing event stream handler
└── lib/
    └── utils.ts                # Utility functions (Nord theme helpers)
```

## Usage

1. **Configure** `SOLAR_CONTROL_URL` and `SOLAR_CONTROL_API_KEY` in the server environment (or `VITE_*` for dev).
2. **Navigate** to the Routing page (default view) to monitor request flow.
3. **Hosts & Instances** - Approve **pending hosts** that registered with solar-control, or add hosts directly. Create and manage instances per host.
4. **Create Instances** - Select backend type (llama.cpp, HuggingFace Causal, Classification, or Embedding).
5. **Manage Instances** - Start, stop, edit, or delete model instances.
6. **View Logs** - Click the log icon on any instance for real-time output.
7. **Monitor Performance** - Watch the routing graph to see load distribution.

## Creating Instances

### llama.cpp Instance
- **Model Path**: Path to GGUF model file
- **Alias**: Model identifier for API routing
- **GPU Layers, Context Size, Threads**: Hardware configuration
- **Sampling Parameters**: Temperature, Top-P, Top-K, Min-P

### HuggingFace Causal LM Instance
- **Model ID**: HuggingFace model ID or local path
- **Alias**: Model identifier for API routing
- **Device**: `auto`, `cuda`, `mps`, or `cpu`
- **Dtype**: `auto`, `float16`, `bfloat16`, or `float32`
- **Max Length**: Maximum sequence length
- **Flash Attention**: Enable for faster inference on compatible GPUs

### HuggingFace Classification Instance
- **Model ID**: HuggingFace model ID or local path
- **Alias**: Model identifier for API routing
- **Device**: `auto`, `cuda`, `mps`, or `cpu`
- **Dtype**: `auto`, `float16`, `bfloat16`, or `float32`
- **Max Length**: Maximum sequence length
- **Labels**: Optional custom label names

### HuggingFace Embedding Instance
- **Model ID**: HuggingFace model ID or local path (e.g., `sentence-transformers/all-MiniLM-L6-v2`)
- **Alias**: Model identifier for API routing
- **Device**: `auto`, `cuda`, `mps`, or `cpu`
- **Dtype**: `auto`, `float16`, `bfloat16`, or `float32`
- **Max Length**: Maximum sequence length
- **Normalize Embeddings**: L2 normalize output vectors (recommended for similarity search)

## Technology Stack

- **React 18** - Modern UI library
- **TypeScript** - Type-safe JavaScript
- **Vite** - Fast build tool and dev server
- **Tailwind CSS** - Utility-first CSS framework with Nord theme
- **React Flow** - Interactive node-based graphs for routing visualization
- **Axios** - HTTP client for API communication
- **Lucide Icons** - Beautiful icon library
- **Zustand** - Lightweight state management
