export function resolveApiBase(
  apiBaseUrl: string | undefined,
  basePath: string,
  origin: string
): URL {
  if (!apiBaseUrl) {
    throw new Error('VITE_API_BASE_URL is not configured. Set it as an environment variable.');
  }
  const mountPoint = new URL(basePath, origin);
  return new URL(apiBaseUrl, mountPoint);
}

// The mount point is injected at build time (vite define, mirroring paths.base);
// import.meta.env.BASE_URL is './' in this toolchain and not the mount point.
const basePath: string = __BASE_PATH__;

export const apiBase: URL = resolveApiBase(
  import.meta.env.VITE_API_BASE_URL,
  basePath,
  location.origin
);

export function createApiUrl(endpointPath: string, base: URL = apiBase): URL {
  const root = `${base}`.replace(/\/+$/, '');
  return new URL(endpointPath.replace(/^\/+/, ''), `${root}/`);
}
