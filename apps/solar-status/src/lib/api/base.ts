const MOUNT_RELATIVE_API_PATH = '../api';

export function resolveApiBase(
  apiBaseUrl: string | undefined,
  basePath: string,
  origin: string
): URL {
  const mountPoint = new URL(basePath, origin);
  return new URL(apiBaseUrl ?? MOUNT_RELATIVE_API_PATH, mountPoint);
}

// Vite exposes the mount point only as its built-in BASE_URL; the app knows it as BASE_PATH.
const basePath: string = import.meta.env.BASE_URL;

export const apiBase: URL = resolveApiBase(
  import.meta.env.VITE_API_BASE_URL,
  basePath,
  location.origin
);

export function apiUrl(endpointPath: string, base: URL = apiBase): URL {
  const root = `${base}`.replace(/\/+$/, '');
  return new URL(endpointPath.replace(/^\/+/, ''), `${root}/`);
}
