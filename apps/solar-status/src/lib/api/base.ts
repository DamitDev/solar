const MOUNT_RELATIVE_API_PATH = '../api';

export function resolveApiBase(
  apiEnvBase: string | undefined,
  mountBase: string,
  origin: string
): URL {
  const mountPoint = new URL(mountBase, origin);
  return new URL(apiEnvBase ?? MOUNT_RELATIVE_API_PATH, mountPoint);
}

export const apiBase: URL = resolveApiBase(
  import.meta.env.VITE_API_BASE,
  import.meta.env.BASE_URL,
  location.origin
);

export function apiUrl(endpointPath: string, base: URL = apiBase): URL {
  const root = `${base}`.replace(/\/+$/, '');
  return new URL(endpointPath.replace(/^\/+/, ''), `${root}/`);
}
