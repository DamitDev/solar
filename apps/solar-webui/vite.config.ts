import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import crypto from 'crypto';

const AUTH_COOKIE_NAME = 'solar_webui_auth';
const AUTH_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60;

const parseCookies = (cookieHeader = '') =>
  cookieHeader
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .reduce<Record<string, string>>((cookies, part) => {
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

const serializeCookie = (name: string, value: string, options: { maxAge?: number; httpOnly?: boolean; sameSite?: string }) => {
  const segments = [`${encodeURIComponent(name)}=${encodeURIComponent(value)}`, 'Path=/'];
  if (options.maxAge !== undefined) segments.push(`Max-Age=${options.maxAge}`);
  if (options.httpOnly) segments.push('HttpOnly');
  if (options.sameSite) segments.push(`SameSite=${options.sameSite}`);
  return segments.join('; ');
};

const hashValue = (value: string) => crypto.createHash('sha256').update(value).digest();

const constantTimeEqual = (left: string, right: string) => crypto.timingSafeEqual(hashValue(left), hashValue(right));

const signAuthTimestamp = (authKey: string, timestamp: string | number) =>
  crypto.createHmac('sha256', authKey).update(String(timestamp)).digest('base64url');

const createAuthToken = (authKey: string) => {
  const issuedAt = Date.now();
  return `v1.${issuedAt}.${signAuthTimestamp(authKey, issuedAt)}`;
};

const isValidAuthToken = (authKey: string, token?: string) => {
  if (!authKey || typeof token !== 'string') return false;

  const [version, issuedAtRaw, signature] = token.split('.');
  if (version !== 'v1' || !issuedAtRaw || !signature) return false;

  const issuedAt = Number.parseInt(issuedAtRaw, 10);
  if (!Number.isFinite(issuedAt)) return false;
  if (Date.now() - issuedAt > AUTH_COOKIE_MAX_AGE_SECONDS * 1000) return false;

  return constantTimeEqual(signature, signAuthTimestamp(authKey, issuedAtRaw));
};

const hasValidAuthCookie = (authKey: string, req: { headers: { cookie?: string } }) => {
  if (!authKey) return true;
  const cookies = parseCookies(req.headers.cookie);
  return isValidAuthToken(authKey, cookies[AUTH_COOKIE_NAME]);
};

const escapeHtml = (value: string) =>
  value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

const safeRedirectTarget = (value: unknown) => {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return '/';
  }
  return value;
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

const readRequestBody = (req: NodeJS.ReadableStream) =>
  new Promise<string>((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => {
      body += chunk;
    });
    req.on('end', () => resolve(body));
    req.on('error', reject);
  });

const webuiAuthPlugin = (authKey: string): Plugin => ({
  name: 'solar-webui-auth',
  configureServer(server) {
    if (!authKey) return;

    server.middlewares.use(async (req, res, next) => {
      const requestUrl = new URL(req.url || '/', 'http://localhost');
      const pathname = requestUrl.pathname;

      if (pathname === '/api/health') {
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ status: 'ok', authEnabled: true, dev: true }));
        return;
      }

      if (pathname === '/auth/login' && req.method === 'GET') {
        if (hasValidAuthCookie(authKey, req)) {
          res.statusCode = 302;
          res.setHeader('Location', safeRedirectTarget(requestUrl.searchParams.get('next')));
          res.end();
          return;
        }

        res.setHeader('Content-Type', 'text/html');
        res.end(renderLoginPage({ next: safeRedirectTarget(requestUrl.searchParams.get('next')) }));
        return;
      }

      if (pathname === '/auth/login' && req.method === 'POST') {
        const body = new URLSearchParams(await readRequestBody(req));
        const nextUrl = safeRedirectTarget(body.get('next'));
        const submittedKey = body.get('auth_key') || '';

        if (!constantTimeEqual(submittedKey, authKey)) {
          res.statusCode = 401;
          res.setHeader('Content-Type', 'text/html');
          res.end(renderLoginPage({ error: 'Invalid auth key.', next: nextUrl }));
          return;
        }

        res.statusCode = 302;
        res.setHeader(
          'Set-Cookie',
          serializeCookie(AUTH_COOKIE_NAME, createAuthToken(authKey), {
            httpOnly: true,
            sameSite: 'Lax',
            maxAge: AUTH_COOKIE_MAX_AGE_SECONDS,
          }),
        );
        res.setHeader('Location', nextUrl);
        res.end();
        return;
      }

      if (pathname === '/auth/logout' && req.method === 'POST') {
        res.statusCode = 302;
        res.setHeader(
          'Set-Cookie',
          serializeCookie(AUTH_COOKIE_NAME, '', {
            httpOnly: true,
            sameSite: 'Lax',
            maxAge: 0,
          }),
        );
        res.setHeader('Location', '/auth/login');
        res.end();
        return;
      }

      if (hasValidAuthCookie(authKey, req)) {
        next();
        return;
      }

      if (pathname.startsWith('/api/')) {
        res.statusCode = 401;
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ error: 'Authentication required' }));
        return;
      }

      res.statusCode = 302;
      res.setHeader('Location', `/auth/login?next=${encodeURIComponent(safeRedirectTarget(req.url || '/'))}`);
      res.end();
    });
  },
});

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const controlUrl = env.SOLAR_CONTROL_URL || 'http://localhost:8000';
  const controlApiKey = env.SOLAR_CONTROL_API_KEY || '';
  const webuiAuthKey = env.SOLAR_WEBUI_AUTH_KEY || '';

  return {
    plugins: [webuiAuthPlugin(webuiAuthKey), react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      proxy: {
        '/api/control': {
          target: controlUrl,
          changeOrigin: true,
          ws: true,
          rewrite: (pathname) => pathname.replace(/^\/api\/control/, ''),
          configure: (proxy) => {
            const applyHeaders = (proxyReq: any) => {
              if (!controlApiKey) return;
              proxyReq.setHeader('X-API-Key', controlApiKey);
              if (!proxyReq.getHeader('authorization')) {
                proxyReq.setHeader('Authorization', `Bearer ${controlApiKey}`);
              }
            };

            proxy.on('proxyReq', applyHeaders);
            proxy.on('proxyReqWs', (proxyReq, req, socket) => {
              if (!hasValidAuthCookie(webuiAuthKey, req)) {
                socket.write('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n');
                socket.destroy();
                proxyReq.destroy();
                return;
              }
              applyHeaders(proxyReq);
            });
          },
        },
      },
    },
  };
});

