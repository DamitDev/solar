import { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { CheckCircle2, CircleX, Loader2, RefreshCw, XCircle } from 'lucide-react';
import solarClient from '@/api/client';
import { CompleteUploadResponse, CreateUploadResponse } from '@/api/types';
import { formatBytes } from '@/lib/utils';
import { SelectedUploadFile } from '@/lib/uploadPaths';

const CONCURRENCY = 3;

type FileStatus = 'queued' | 'uploading' | 'done' | 'failed';

interface FileProgress {
  path: string;
  sentBytes: number;
  totalBytes: number;
  status: FileStatus;
  error?: string;
}

interface ProgressStepProps {
  session: CreateUploadResponse;
  entries: SelectedUploadFile[];
  onCancel: () => void;
}

export function ProgressStep({ session, entries, onCancel }: ProgressStepProps) {
  const [progress, setProgress] = useState<Record<string, FileProgress>>(() => {
    const initial: Record<string, FileProgress> = {};
    for (const entry of entries) {
      initial[entry.path] = {
        path: entry.path,
        sentBytes: 0,
        totalBytes: entry.file.size,
        status: 'queued',
      };
    }
    return initial;
  });
  const [overallError, setOverallError] = useState<string | null>(null);
  const [completing, setCompleting] = useState(false);
  const [result, setResult] = useState<CompleteUploadResponse | null>(null);
  const abortedRef = useRef(false);
  const pumpRef = useRef<boolean>(false);

  const patchProgress = useCallback((path: string, patch: Partial<FileProgress>) => {
    setProgress((prev) => ({ ...prev, [path]: { ...prev[path], ...patch } }));
  }, []);

  const uploadOne = useCallback(
    async (entry: SelectedUploadFile): Promise<boolean> => {
      patchProgress(entry.path, { status: 'uploading', error: undefined });
      try {
        const result = await solarClient.uploadFile(session.upload_id, entry.path, entry.file, (sentBytes) =>
          patchProgress(entry.path, { sentBytes }),
        );
        patchProgress(entry.path, { status: 'done', sentBytes: result.size });
        return true;
      } catch (error: any) {
        patchProgress(entry.path, {
          status: 'failed',
          error: error?.message || 'Upload failed',
        });
        return false;
      }
    },
    [session.upload_id, patchProgress],
  );

  // Pump: keep up to CONCURRENCY files in flight until the queue empties.
  const pump = useCallback(
    async (queueRef: React.MutableRefObject<SelectedUploadFile[]>) => {
      if (pumpRef.current) return;
      pumpRef.current = true;
      try {
        while (queueRef.current.length > 0 && !abortedRef.current) {
          const batch = queueRef.current.splice(0, CONCURRENCY);
          await Promise.all(batch.map((entry) => uploadOne(entry)));
        }
      } finally {
        pumpRef.current = false;
      }
    },
    [uploadOne],
  );

  const queueRef = useRef<SelectedUploadFile[]>([]);

  const startUploads = useCallback(() => {
    queueRef.current = entries.filter((entry) => {
      const state = progressRef.current[entry.path];
      return state === undefined || state.status === 'queued' || state.status === 'failed';
    });
    void pump(queueRef);
  }, [entries, pump]);

  // Keep the latest progress for the queue filter without re-subscribing.
  const progressRef = useRef(progress);
  progressRef.current = progress;

  useEffect(() => {
    startUploads();
  }, [startUploads]);

  // Guard navigation while anything is still in flight (spec §5.3).
  const inFlight = Object.values(progress).some((state) => state.status === 'queued' || state.status === 'uploading');
  useEffect(() => {
    if (!inFlight) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [inFlight]);

  // When every file is done, complete the session.
  const allDone = Object.keys(progress).length > 0 && Object.values(progress).every((state) => state.status === 'done');

  useEffect(() => {
    if (!allDone || completing || result) return;
    setCompleting(true);
    solarClient
      .completeUpload(session.upload_id)
      .then((completed) => setResult(completed))
      .catch((error: any) => {
        setOverallError(error?.response?.data?.detail || error?.message || 'Completion failed');
        setCompleting(false);
      });
  }, [allDone, completing, result, session.upload_id]);

  const handleRetry = (path: string) => {
    setOverallError(null);
    const entry = entries.find((candidate) => candidate.path === path);
    if (!entry) return;
    patchProgress(path, { status: 'queued', sentBytes: 0, error: undefined });
    queueRef.current.push(entry);
    void pump(queueRef);
  };

  const handleRetryAll = () => {
    setOverallError(null);
    for (const entry of entries) {
      if (progressRef.current[entry.path]?.status === 'failed') {
        patchProgress(entry.path, { status: 'queued', sentBytes: 0, error: undefined });
        queueRef.current.push(entry);
      }
    }
    void pump(queueRef);
  };

  const handleAbort = async () => {
    abortedRef.current = true;
    try {
      await solarClient.abortUpload(session.upload_id);
    } catch {
      /* session may already be gone; the local abort still stands */
    }
    onCancel();
  };

  if (result) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-xl border border-nord-14/60 bg-nord-14/10 p-10 text-center">
        <CheckCircle2 size={40} className="text-nord-14" />
        <h3 className="text-lg font-semibold text-nord-6">Upload complete</h3>
        <p className="text-sm text-nord-4">
          {result.category}{' '}
          <span className="font-mono">
            {result.name}:{result.version}
          </span>{' '}
          registered · {formatBytes(result.size_bytes)}
        </p>
        <div className="mt-2 flex gap-3">
          <Link
            to="/catalog"
            className="rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9"
          >
            View in catalog
          </Link>
        </div>
      </div>
    );
  }

  const failedCount = Object.values(progress).filter((state) => state.status === 'failed').length;
  const doneCount = Object.values(progress).filter((state) => state.status === 'done').length;
  const totalBytes = entries.reduce((sum, entry) => sum + entry.file.size, 0);
  const sentBytes = Object.values(progress).reduce((sum, state) => sum + state.sentBytes, 0);
  const percent = totalBytes > 0 ? Math.round((sentBytes / totalBytes) * 100) : 0;

  return (
    <div className="space-y-6">
      <div className="rounded-xl border border-nord-3 bg-nord-1 p-5">
        <div className="flex items-center justify-between text-sm">
          <span className="text-nord-6 font-medium">
            {doneCount}/{entries.length} files · {formatBytes(sentBytes)} / {formatBytes(totalBytes)}
          </span>
          <span className="text-nord-4">{percent}%</span>
        </div>
        <div className="mt-3 h-2 rounded-full bg-nord-3 overflow-hidden">
          <div className="h-full rounded-full bg-nord-10 transition-all" style={{ width: `${percent}%` }} />
        </div>
      </div>

      {overallError && (
        <div className="flex items-start gap-2 rounded-xl border border-nord-11/60 bg-nord-11/10 p-4 text-sm text-nord-11">
          <CircleX size={18} className="mt-0.5 shrink-0" />
          <span>{overallError}</span>
        </div>
      )}

      <div className="rounded-xl border border-nord-3 bg-nord-1 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-nord-4 border-b border-nord-3">
              <th className="px-5 py-2 font-medium">File</th>
              <th className="px-5 py-2 font-medium">Size</th>
              <th className="px-5 py-2 font-medium">Progress</th>
              <th className="px-5 py-2 font-medium text-right">Status</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => {
              const state = progress[entry.path];
              if (!state) return null;
              return (
                <tr key={entry.path} className="border-b border-nord-3 last:border-0">
                  <td className="px-5 py-2 font-mono text-xs text-nord-6">{entry.path}</td>
                  <td className="px-5 py-2 text-nord-4">{formatBytes(entry.file.size)}</td>
                  <td className="px-5 py-2">
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 w-32 rounded-full bg-nord-3 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-nord-10 transition-all"
                          style={{
                            width: `${
                              state.totalBytes > 0 ? Math.round((state.sentBytes / state.totalBytes) * 100) : 0
                            }%`,
                          }}
                        />
                      </div>
                      <span className="text-xs text-nord-4">{formatBytes(state.sentBytes)}</span>
                    </div>
                  </td>
                  <td className="px-5 py-2 text-right">
                    {state.status === 'done' && (
                      <span className="inline-flex items-center gap-1 text-xs text-nord-14">
                        <CheckCircle2 size={14} /> done
                      </span>
                    )}
                    {state.status === 'uploading' && (
                      <span className="inline-flex items-center gap-1 text-xs text-nord-10">
                        <Loader2 size={14} className="animate-spin" /> uploading
                      </span>
                    )}
                    {state.status === 'queued' && <span className="text-xs text-nord-4">queued</span>}
                    {state.status === 'failed' && (
                      <span className="inline-flex items-center gap-2 text-xs text-nord-11">
                        <XCircle size={14} /> {state.error || 'failed'}
                        <button
                          type="button"
                          onClick={() => handleRetry(entry.path)}
                          className="inline-flex items-center gap-1 rounded border border-nord-3 px-2 py-0.5 text-nord-4 hover:bg-nord-2"
                        >
                          <RefreshCw size={12} /> retry
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={handleAbort}
          className="rounded-lg border border-nord-11/60 px-4 py-2 text-sm font-medium text-nord-11 transition-colors hover:bg-nord-11/10"
        >
          Abort upload
        </button>
        {failedCount > 0 && (
          <button
            type="button"
            onClick={handleRetryAll}
            className="inline-flex items-center gap-1 rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9"
          >
            <RefreshCw size={14} /> Retry {failedCount} failed
          </button>
        )}
        {completing && (
          <span className="inline-flex items-center gap-2 text-sm text-nord-4">
            <Loader2 size={16} className="animate-spin" /> Registering version…
          </span>
        )}
      </div>
    </div>
  );
}
