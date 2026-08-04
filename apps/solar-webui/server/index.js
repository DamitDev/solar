import express from 'express';
import compression from 'compression';
import { createProxyMiddleware } from 'http-proxy-middleware';
import dotenv from 'dotenv';
import path from 'path';
import fs from 'fs';
import http from 'http';
import https from 'https';
import crypto from 'crypto';
import { fileURLToPath } from 'url';

dotenv.config();

const PORT = Number.parseInt(process.env.PORT || process.env.SOLAR_WEBUI_PORT || '8080', 10);
const CONTROL_URL = process.env.SOLAR_CONTROL_URL || 'http://localhost:8000';
const CONTROL_API_KEY = process.env.SOLAR_CONTROL_API_KEY || '';
const WEBUI_AUTH_KEY = process.env.SOLAR_WEBUI_AUTH_KEY || '';
const LOG_LEVEL = process.env.NODE_ENV === 'production' ? 'warn' : 'info';
const DEBUG_PROXY = process.env.SOLAR_WEBUI_DEBUG === 'true';
const AUTH_COOKIE_NAME = 'solar_webui_auth';
const AUTH_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const packageJson = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../package.json'), 'utf-8'));
const APP_VERSION = process.env.APP_VERSION || packageJson.version || '0.0.0-dev';

console.log('[solar-webui] config', {
  version: APP_VERSION,
  port: PORT,
  controlUrl: CONTROL_URL,
  hasControlApiKey: CONTROL_API_KEY.length > 0,
  authEnabled: WEBUI_AUTH_KEY.length > 0,
});

const DIST_DIR = path.resolve(__dirname, '../dist');

const app = express();
app.disable('x-powered-by');
app.set('etag', false); // Disable ETag generation for proxied requests
app.set('trust proxy', true);

const isAuthEnabled = () => WEBUI_AUTH_KEY.length > 0;

const safeRedirectTarget = (value) => {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return '/';
  }
  return value;
};

const escapeHtml = (value) =>
  String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

const parseCookies = (cookieHeader = '') =>
  cookieHeader
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .reduce((cookies, part) => {
      const separatorIndex = part.indexOf('=');
      if (separatorIndex === -1) return cookies;
      try {
        const name = decodeURIComponent(part.slice(0, separatorIndex));
        const value = decodeURIComponent(part.slice(separatorIndex + 1));
        cookies[name] = value;
      } catch {
        // Ignore malformed cookie pairs instead of failing the whole request.
      }
      return cookies;
    }, {});

const serializeCookie = (name, value, options = {}) => {
  const segments = [`${encodeURIComponent(name)}=${encodeURIComponent(value)}`, `Path=${options.path || '/'}`];
  if (options.maxAge !== undefined) segments.push(`Max-Age=${options.maxAge}`);
  if (options.httpOnly) segments.push('HttpOnly');
  if (options.secure) segments.push('Secure');
  if (options.sameSite) segments.push(`SameSite=${options.sameSite}`);
  return segments.join('; ');
};

const hashValue = (value) => crypto.createHash('sha256').update(value).digest();

const constantTimeEqual = (left, right) => crypto.timingSafeEqual(hashValue(left), hashValue(right));

const signAuthTimestamp = (timestamp) =>
  crypto.createHmac('sha256', WEBUI_AUTH_KEY).update(String(timestamp)).digest('base64url');

const createAuthToken = () => {
  const issuedAt = Date.now();
  return `v1.${issuedAt}.${signAuthTimestamp(issuedAt)}`;
};

const isValidAuthToken = (token) => {
  if (!isAuthEnabled() || typeof token !== 'string') return false;

  const [version, issuedAtRaw, signature] = token.split('.');
  if (version !== 'v1' || !issuedAtRaw || !signature) return false;

  const issuedAt = Number.parseInt(issuedAtRaw, 10);
  if (!Number.isFinite(issuedAt)) return false;
  if (Date.now() - issuedAt > AUTH_COOKIE_MAX_AGE_SECONDS * 1000) return false;

  return constantTimeEqual(signature, signAuthTimestamp(issuedAtRaw));
};

const hasValidAuthCookie = (req) => {
  if (!isAuthEnabled()) return true;
  const cookies = parseCookies(req.headers.cookie);
  return isValidAuthToken(cookies[AUTH_COOKIE_NAME]);
};

const shouldUseSecureCookie = (req) => req.secure || req.headers['x-forwarded-proto'] === 'https';

const setAuthCookie = (req, res) => {
  res.setHeader(
    'Set-Cookie',
    serializeCookie(AUTH_COOKIE_NAME, createAuthToken(), {
      httpOnly: true,
      sameSite: 'Lax',
      secure: shouldUseSecureCookie(req),
      maxAge: AUTH_COOKIE_MAX_AGE_SECONDS,
    }),
  );
};

const clearAuthCookie = (req, res) => {
  res.setHeader(
    'Set-Cookie',
    serializeCookie(AUTH_COOKIE_NAME, '', {
      httpOnly: true,
      sameSite: 'Lax',
      secure: shouldUseSecureCookie(req),
      maxAge: 0,
    }),
  );
};

const renderLoginPage = ({ error = '', next = '/' } = {}) => `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Solar WebUI Login</title>
    <style>
      :root {
        color-scheme: dark;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #2e3440;
        color: #eceff4;
      }
      body {
        min-height: 100vh;
        margin: 0;
        display: grid;
        place-items: center;
        background: radial-gradient(circle at top, #3b4252 0, #2e3440 55%);
      }
      main {
        width: min(100% - 32px, 420px);
        padding: 32px;
        border: 1px solid #4c566a;
        border-radius: 18px;
        background: rgba(46, 52, 64, 0.92);
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      }
      h1 {
        margin: 0 0 8px;
        font-size: 1.5rem;
      }
      p {
        margin: 0 0 24px;
        color: #d8dee9;
      }
      label {
        display: block;
        margin-bottom: 8px;
        color: #d8dee9;
        font-size: 0.9rem;
      }
      input {
        width: 100%;
        box-sizing: border-box;
        padding: 12px 14px;
        border: 1px solid #4c566a;
        border-radius: 10px;
        background: #3b4252;
        color: #eceff4;
        font-size: 1rem;
      }
      button {
        width: 100%;
        margin-top: 18px;
        padding: 12px 14px;
        border: 0;
        border-radius: 10px;
        background: #5e81ac;
        color: #eceff4;
        font-weight: 700;
        cursor: pointer;
      }
      .error {
        margin: 0 0 16px;
        color: #bf616a;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>Solar WebUI</h1>
      <p>Enter the maintenance key to continue.</p>
      ${error ? `<p class="error">${escapeHtml(error)}</p>` : ''}
      <form method="post" action="/auth/login">
        <input type="hidden" name="next" value="${escapeHtml(next)}" />
        <label for="auth_key">Auth key</label>
        <input id="auth_key" name="auth_key" type="password" autocomplete="current-password" autofocus required />
        <button type="submit">Unlock</button>
      </form>
    </main>
  </body>
</html>`;

const requireAuth = (req, res, next) => {
  if (hasValidAuthCookie(req)) {
    return next();
  }

  if (req.path.startsWith('/api/')) {
    return res.status(401).json({ error: 'Authentication required' });
  }

  const nextUrl = safeRedirectTarget(req.originalUrl || '/');
  return res.redirect(`/auth/login?next=${encodeURIComponent(nextUrl)}`);
};

app.use(express.urlencoded({ extended: false }));

app.get('/auth/login', (req, res) => {
  if (!isAuthEnabled() || hasValidAuthCookie(req)) {
    return res.redirect(safeRedirectTarget(req.query.next));
  }

  return res.type('html').send(renderLoginPage({ next: safeRedirectTarget(req.query.next) }));
});

app.post('/auth/login', (req, res) => {
  if (!isAuthEnabled()) {
    return res.redirect('/');
  }

  const nextUrl = safeRedirectTarget(req.body?.next);
  const submittedKey = typeof req.body?.auth_key === 'string' ? req.body.auth_key : '';
  if (!constantTimeEqual(submittedKey, WEBUI_AUTH_KEY)) {
    return res.status(401).type('html').send(renderLoginPage({ error: 'Invalid auth key.', next: nextUrl }));
  }

  setAuthCookie(req, res);
  return res.redirect(nextUrl);
});

app.post('/auth/logout', (req, res) => {
  clearAuthCookie(req, res);
  return res.redirect('/auth/login');
});

app.get('/api/health', (_req, res) => {
  res.json({
    status: 'ok',
    version: APP_VERSION,
    target: CONTROL_URL,
    hasControlApiKey: Boolean(CONTROL_API_KEY),
    authEnabled: isAuthEnabled(),
  });
});

app.use(requireAuth);

// Create HTTP/HTTPS agents with keep-alive for connection reuse
// This significantly reduces latency by reusing TCP connections
const httpAgent = new http.Agent({
  keepAlive: true,
  keepAliveMsecs: 30000,
  maxSockets: 50,
  maxFreeSockets: 10,
  timeout: 30000
});

const httpsAgent = new https.Agent({
  keepAlive: true,
  keepAliveMsecs: 30000,
  maxSockets: 50,
  maxFreeSockets: 10,
  timeout: 30000
});

const controlProxy = createProxyMiddleware({
  target: CONTROL_URL,
  changeOrigin: true,
  ws: true,
  // Use keep-alive agents for connection reuse
  agent: CONTROL_URL.startsWith('https') ? httpsAgent : httpAgent,
  logLevel: LOG_LEVEL,
  pathRewrite: (path) => path.replace(/^\/api\/control/, ''),
  preserveHeaderKeyCase: true,
  // Performance optimizations
  followRedirects: false,
  xfwd: true,
  proxyTimeout: 30000,
  timeout: 30000,
  headers: CONTROL_API_KEY
    ? {
        'X-API-Key': CONTROL_API_KEY,
        Authorization: `Bearer ${CONTROL_API_KEY}`,
      }
    : undefined,
  onProxyReq: (proxyReq, req, res) => {
    const startTime = Date.now();
    req._proxyStartTime = startTime;
    if (DEBUG_PROXY) {
      console.log('[proxy] incoming request', {
        method: req.method,
        url: req.originalUrl,
        hasApiKey: !!req.headers['x-api-key'],
      });
    }
  },
  onProxyRes: (proxyRes, req, res) => {
    const duration = Date.now() - (req._proxyStartTime || 0);
    if (DEBUG_PROXY) {
      console.log('[proxy] response', {
        method: req.method,
        url: req.originalUrl,
        status: proxyRes.statusCode,
        duration: `${duration}ms`
      });
    }
  },
  onError: (err, req, res) => {
    const duration = Date.now() - (req._proxyStartTime || 0);
    console.error(`[proxy] error after ${duration}ms:`, {
      method: req.method,
      url: req.originalUrl,
      error: err.message
    });
  }
});

app.use('/api/control', controlProxy);

// Runtime config injection — the standard pattern for SPA + Docker.
// Vite bakes VITE_* env vars at build time, so they're empty in a Docker
// image built without them.  Instead the Express server injects a small
// <script> tag with runtime values into the served index.html.
const buildRuntimeHtml = () => {
  const indexPath = path.join(DIST_DIR, 'index.html');
  if (!fs.existsSync(indexPath)) return null;
  const raw = fs.readFileSync(indexPath, 'utf-8');
  const cfg = {
    SOLAR_CONTROL_API_KEY: CONTROL_API_KEY || '',
  };
  const tag = `<script>window.__SOLAR_CONFIG__=${JSON.stringify(cfg)};</script>`;
  return raw.replace('</head>', `${tag}\n</head>`);
};

if (fs.existsSync(DIST_DIR)) {
  const runtimeHtml = buildRuntimeHtml();
  app.use(compression());
  app.use(
    express.static(DIST_DIR, {
      index: false,
      maxAge: '1h',
    }),
  );

  app.use((req, res, next) => {
    if (req.method !== 'GET') {
      return next();
    }
    if (runtimeHtml) {
      res.type('html').send(runtimeHtml);
    } else {
      res.sendFile(path.join(DIST_DIR, 'index.html'));
    }
  });
} else {
  app.use((_req, res) => {
    res.status(500).send('Build output missing. Run "npm run build" before starting the server.');
  });
}

const server = http.createServer(app);

server.on('upgrade', (req, socket, head) => {
  if (!req.url?.startsWith('/api/control')) {
    socket.destroy();
    return;
  }

  if (!hasValidAuthCookie(req)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
    socket.destroy();
    return;
  }

  // http-proxy-middleware's `headers` config only applies to regular HTTP
  // requests, NOT WebSocket upgrades.  Inject auth headers manually so
  // solar-control can authenticate the proxied upgrade.
  if (CONTROL_API_KEY) {
    req.headers['x-api-key'] = CONTROL_API_KEY;
    req.headers['authorization'] = `Bearer ${CONTROL_API_KEY}`;
  }

  if (DEBUG_PROXY) {
    console.log('[proxy] upgrade request', {
      url: req.url,
      headers: {
        host: req.headers.host,
        origin: req.headers.origin,
        hasApiKey: !!req.headers['x-api-key'],
      },
    });
  }
  
  if (typeof controlProxy.upgrade === 'function') {
    controlProxy.upgrade(req, socket, head);
  } else {
    console.warn('[proxy] controlProxy.upgrade is not a function');
    socket.destroy();
  }
});

server.listen(PORT, () => {
  console.log(`[solar-webui] listening on port ${PORT}`);
  console.log(`[solar-webui] proxying control requests to ${CONTROL_URL}`);
  if (!CONTROL_API_KEY) {
    console.warn('[solar-webui] SOLAR_CONTROL_API_KEY is not set. API requests may fail with 401.');
  }
});
