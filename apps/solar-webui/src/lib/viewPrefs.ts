/**
 * Small wrapper around the `solar_*` localStorage convention already used for
 * view mode and host order, so a page with several preferences does not repeat
 * the same try/catch and validation five times.
 */

export function readPref<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw != null && (allowed as readonly string[]).includes(raw)) return raw as T;
  } catch {
    /* storage unavailable (private mode, disabled cookies) — use the default */
  }
  return fallback;
}

export function writePref(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* preference simply will not persist */
  }
}
