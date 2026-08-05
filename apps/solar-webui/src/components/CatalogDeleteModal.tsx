/**
 * CatalogDeleteModal — confirm and report a catalog delete (U-008).
 *
 * Two phases mirroring StorageDeleteModal: a confirm phase (with a blocked
 * state rendering the running-instance warning and a disabled confirm, and
 * typed name confirmation for whole-repository delete) and a result/error
 * phase reporting per-version outcomes on partial failure.
 */

import { useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, Loader2, Trash2, X, XCircle } from 'lucide-react';
import solarClient from '@/api/client';
import { CatalogDeleteResult } from '@/api/types';

export interface CatalogDeleteTarget {
  kind: 'version' | 'repository';
  version?: string;
  /** Pre-known blocker count — the confirm stays disabled while > 0. */
  blockedByRunning?: number;
}

interface CatalogDeleteModalProps {
  modelName: string;
  target: CatalogDeleteTarget;
  onClose: () => void;
  /** Called when the modal closes after a completed delete — refetch. */
  onDone: () => void;
}

type Phase = 'confirm' | 'error' | 'result';

export function CatalogDeleteModal({ modelName, target, onClose, onDone }: CatalogDeleteModalProps) {
  const [phase, setPhase] = useState<Phase>('confirm');
  const [deleting, setDeleting] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [result, setResult] = useState<CatalogDeleteResult | null>(null);
  const [typedName, setTypedName] = useState('');

  const isRepo = target.kind === 'repository';
  const blocked = (target.blockedByRunning ?? 0) > 0;
  const typedConfirmed = !isRepo || typedName === modelName;
  const confirmDisabled = deleting || blocked || !typedConfirmed;

  const blockedText = useMemo(() => {
    if (!blocked) return null;
    if (isRepo) {
      return `${target.blockedByRunning} running instance${
        target.blockedByRunning === 1 ? '' : 's'
      } serve${target.blockedByRunning === 1 ? 's' : ''} this model. Stop or undeploy them first.`;
    }
    return `${target.blockedByRunning} running instance${target.blockedByRunning === 1 ? '' : 's'} serve${
      target.blockedByRunning === 1 ? 's' : ''
    } version ${target.version}. Stop or undeploy them first.`;
  }, [blocked, isRepo, target]);

  const handleDelete = async () => {
    setDeleting(true);
    setErrorDetail(null);
    try {
      if (isRepo) {
        setResult(await solarClient.deleteCatalogModel(modelName));
      } else {
        await solarClient.deleteCatalogModelVersion(modelName, target.version!);
        setResult({
          name: modelName,
          deleted: [target.version!],
          failed: [],
          artifact_removed: false,
          harbor_repository_removed: false,
        });
      }
      setPhase('result');
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      setErrorDetail(typeof detail === 'string' ? detail : err?.message || 'Delete request failed');
      setPhase('error');
    } finally {
      setDeleting(false);
    }
  };

  const closeAfterDone = () => {
    onClose();
    onDone();
  };

  return (
    <div
      role="dialog"
      aria-label={isRepo ? 'Delete model repository' : `Delete version ${target.version}`}
      className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4"
    >
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-2xl w-full border border-nord-3">
        <div className="flex items-center justify-between p-4 border-b border-nord-3">
          <h2 className="text-lg font-bold text-nord-6">
            {isRepo ? 'Delete model repository' : `Delete version ${target.version}`}
          </h2>
          <button
            onClick={onClose}
            disabled={deleting}
            className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4 disabled:opacity-50"
          >
            <X size={18} />
          </button>
        </div>

        {phase === 'confirm' && (
          <div className="p-4 space-y-4">
            <p className="text-sm text-nord-6 break-all">
              {isRepo ? (
                <>
                  <span className="font-medium">{modelName}</span> will be removed from the Data Repository and its
                  artifacts will be deleted from Harbor. This cannot be undone.
                </>
              ) : (
                <>
                  Version <span className="font-medium">{target.version}</span> of{' '}
                  <span className="font-medium">{modelName}</span> will be removed from the catalog and deleted from
                  Harbor. This cannot be undone.
                </>
              )}
            </p>

            <div className="bg-nord-13 bg-opacity-15 border border-nord-13 rounded p-3 text-sm text-nord-6">
              Copies already downloaded to hosts remain until they are evicted through the storage management flow.
            </div>

            {blocked && (
              <div className="bg-nord-12 bg-opacity-15 border border-nord-12 rounded p-3 text-sm text-nord-6">
                <AlertTriangle size={16} className="inline mr-1.5 align-[-3px]" />
                {blockedText}
              </div>
            )}

            {isRepo && (
              <div>
                <label className="block text-sm text-nord-4 mb-1.5">
                  Type <span className="font-mono text-nord-6">{modelName}</span> to confirm
                </label>
                <input
                  value={typedName}
                  onChange={(e) => setTypedName(e.target.value)}
                  disabled={deleting || blocked}
                  placeholder={modelName}
                  className="bg-nord-0 text-nord-6 border border-nord-3 rounded px-3 py-2 w-full focus:border-nord-8 outline-none disabled:opacity-50 font-mono text-sm"
                />
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2 border-t border-nord-3">
              <button
                onClick={onClose}
                disabled={deleting}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleDelete}
                disabled={confirmDisabled}
                className="bg-nord-11 text-nord-6 rounded-md px-4 py-2 flex items-center gap-2 hover:bg-opacity-90 transition-colors disabled:opacity-60"
              >
                {deleting ? (
                  <>
                    <Loader2 size={16} className="animate-spin" /> Deleting…
                  </>
                ) : (
                  <>
                    <Trash2 size={16} /> Delete {isRepo ? 'repository' : 'version'}
                  </>
                )}
              </button>
            </div>
          </div>
        )}

        {phase === 'error' && (
          <div className="p-4 space-y-4">
            <div className="flex items-start gap-2 bg-nord-11 bg-opacity-15 border border-nord-11 rounded p-3 text-sm text-nord-6">
              <XCircle size={18} className="flex-shrink-0 mt-0.5 text-nord-11" />
              <span className="break-all">{errorDetail}</span>
            </div>
            <div className="flex justify-end gap-2 pt-2 border-t border-nord-3">
              <button
                onClick={() => setPhase('confirm')}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors"
              >
                Back
              </button>
              <button
                onClick={onClose}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        )}

        {phase === 'result' && result && (
          <div className="p-4 space-y-3">
            {result.failed.length === 0 ? (
              <div className="flex items-start gap-2 bg-nord-14 bg-opacity-15 border border-nord-14 rounded p-3 text-sm text-nord-6">
                <CheckCircle2 size={18} className="flex-shrink-0 mt-0.5 text-nord-14" />
                <span className="break-all">
                  {isRepo
                    ? `Deleted ${result.deleted.length} version${
                        result.deleted.length === 1 ? '' : 's'
                      }${result.artifact_removed ? ' and removed the repository entry' : ''}.`
                    : `Version ${target.version} deleted.`}
                </span>
              </div>
            ) : (
              <>
                <div className="bg-nord-11 bg-opacity-15 border border-nord-11 rounded p-3 text-sm text-nord-6">
                  <XCircle size={16} className="inline mr-1.5 align-[-3px]" />
                  Some versions could not be deleted. The repository entry was kept so you can retry.
                </div>
                <div className="max-h-48 overflow-y-auto divide-y divide-nord-3 border border-nord-3 rounded">
                  {result.deleted.map((v) => (
                    <div key={v} className="flex items-center gap-2 px-3 py-2 text-sm">
                      <CheckCircle2 size={16} className="text-nord-14 flex-shrink-0" />
                      <span className="text-nord-6 break-all flex-1">{v}</span>
                      <span className="text-xs text-nord-4">Deleted</span>
                    </div>
                  ))}
                  {result.failed.map((f) => (
                    <div key={f.version} className="flex items-center gap-2 px-3 py-2 text-sm">
                      <XCircle size={16} className="text-nord-11 flex-shrink-0" />
                      <span className="text-nord-6 break-all flex-1">{f.version}</span>
                      <span className="text-xs text-nord-4 break-all max-w-64 text-right">{f.detail}</span>
                    </div>
                  ))}
                </div>
                {!result.artifact_removed && (
                  <p className="text-sm text-nord-4">
                    The repository entry remains in the catalog — fix the failures and try again.
                  </p>
                )}
              </>
            )}
            <div className="flex justify-end pt-2 border-t border-nord-3">
              <button
                onClick={closeAfterDone}
                className="bg-nord-3 text-nord-6 rounded-md px-4 py-2 hover:bg-nord-2 transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
