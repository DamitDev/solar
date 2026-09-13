/** C4: model pull progress pushed by a host and rebroadcast by control. */
export interface PullProgressData {
  source_uri: string;
  phase: 'resolving' | 'downloading' | 'verifying' | 'finalizing' | 'completed' | 'failed' | string;
  bytes_done?: number | null;
  bytes_total?: number | null;
  speed_bps?: number | null;
  error?: string | null;
}

export interface PullProgressEvent {
  host_id: string;
  host_name?: string | null;
  timestamp?: string;
  data: PullProgressData;
}

/** A pull that ended: nothing further will arrive on this key. */
export function isTerminalPullPhase(phase: string | undefined): boolean {
  return phase === 'completed' || phase === 'failed';
}

/**
 * Labels for the phases a pull passes through before it ends (C4).
 *
 * Only `downloading` carries byte counts; the others report progress by name
 * alone, and `verifying` in particular walks the whole artifact, so a large
 * model can sit there long enough that showing nothing reads as a stall.
 */
export const PULL_PHASE_LABELS: Record<string, string> = {
  resolving: 'Resolving',
  downloading: 'Downloading',
  verifying: 'Verifying',
  finalizing: 'Finalizing',
};

/**
 * How long a finished pull stays visible before it is dropped (C4).
 *
 * Deliberately shorter than the server's `pull_progress_terminal_grace_s`
 * (300 s). The server grace exists so a client that loads mid-pull still gets
 * the outcome from `GET /api/pulls`; this one only has to cover how long a
 * viewer already on the page should keep seeing "completed" before the row
 * disappears.
 */
export const PULL_PROGRESS_TERMINAL_GRACE_MS = 60_000;
/**
 * How long a pull may go silent before it stops counting as in flight.
 *
 * Mirrors the server's `pull_progress_stale_after_s` (180 s) plus the same
 * margin `GET /api/pulls` applies, so a host that dies mid-download does not
 * leave a frozen byte count on screen — and so the two sides do not disagree
 * about which pulls are live.
 */
export const PULL_PROGRESS_STALE_MS = 360_000;
/** Hard ceiling on tracked pulls, mirroring the per-instance log cap. */
const MAX_PULL_PROGRESS_ENTRIES = 200;

/**
 * Keep the pull-progress map bounded.
 *
 * Every (host, model) pair a session ever sees adds a key that nothing else
 * removes, and a stale entry would otherwise be reported as current forever.
 * Finished pulls age out after a grace so a late-arriving viewer still sees
 * the outcome; unfinished ones age out once the host has stopped reporting,
 * which is the only signal a host that died mid-pull ever gives. *keepKey* is
 * never dropped so the entry just written always survives.
 */
export function prunePullProgress(
  entries: Map<string, PullProgressEvent>,
  keepKey?: string,
  now: number = Date.now(),
): Map<string, PullProgressEvent> {
  for (const [key, entry] of entries) {
    if (key === keepKey) continue;
    const terminal = isTerminalPullPhase(entry.data?.phase);
    const at = entry.timestamp ? Date.parse(entry.timestamp) : NaN;
    if (Number.isNaN(at)) {
      // A terminal entry we cannot age is exactly the one that would linger
      // forever. An unstamped in-flight one may simply be brand new, so it is
      // left to the size cap below rather than guessed at.
      if (terminal) entries.delete(key);
      continue;
    }
    const limit = terminal ? PULL_PROGRESS_TERMINAL_GRACE_MS : PULL_PROGRESS_STALE_MS;
    if (now - at > limit) {
      entries.delete(key);
    }
  }
  if (entries.size <= MAX_PULL_PROGRESS_ENTRIES) return entries;
  const oldestFirst = [...entries.entries()].sort((a, b) => (a[1].timestamp ?? '').localeCompare(b[1].timestamp ?? ''));
  for (const [key] of oldestFirst) {
    if (entries.size <= MAX_PULL_PROGRESS_ENTRIES) break;
    if (key !== keepKey) entries.delete(key);
  }
  return entries;
}
