/**
 * NewIntentModal — declarative intent submission form (U-003).
 *
 * Fields per spec deployment-intent.md §4.1: alias, model_source, replicas,
 * priority, strategy, backend (shared editor), placement, resources, metadata.
 * Client-side shape rules (§4.7) run before submit; the server stays
 * authoritative. Server errors surface as a red alert: 409 = alias conflict,
 * 422 = structured errors[] list.
 */

import { useEffect, useMemo, useState } from 'react';
import { X, Plus, Trash2, ChevronDown } from 'lucide-react';
import solarClient from '@/api/client';
import { Host, Intent, IntentCreateRequest, IntentPriority, IntentStrategy } from '@/api/types';
import { extractApiError } from '@/lib/apiErrors';
import { validateIntentRequest, sanitizeIntentBackend } from '@/lib/intentValidation';
import { getDefaultConfig, stripEmptyOptionalFields } from '@/lib/backendConfig';
import { BackendConfigFields } from './BackendConfigFields';

interface NewIntentModalProps {
  initial?: Partial<IntentCreateRequest>;
  onClose: () => void;
  onCreated: (intent: Intent) => void;
}

interface MetadataRow {
  key: string;
  value: string;
}

/** Ready-made patterns for the most common filtered HuggingFace pulls. */
const FILTER_SUGGESTIONS = ['*UD-Q4_K_XL*', '*Q8_0*', 'mmproj-BF16.gguf', '*.safetensors', 'tokenizer*'];

const isHuggingFaceSource = (source: string) => source.trim().startsWith('huggingface://');

const PRIORITY_EXPLANATIONS: Record<IntentPriority, string> = {
  production: 'Never displaced automatically; may displace lower priorities.',
  staging: 'May be displaced by production; migrates when possible.',
  ephemeral: 'Lowest priority — first to be stopped or migrated when capacity is needed.',
};

export function NewIntentModal({ initial, onClose, onCreated }: NewIntentModalProps) {
  const [loading, setLoading] = useState(false);
  const [alias, setAlias] = useState(initial?.alias ?? '');
  const [modelSource, setModelSource] = useState(initial?.model_source ?? '');
  const [replicas, setReplicas] = useState<number>(initial?.replicas ?? 1);
  const [priority, setPriority] = useState<IntentPriority>(initial?.priority ?? 'production');
  const [strategy, setStrategy] = useState<IntentStrategy>(initial?.strategy ?? 'rolling');
  const [backend, setBackend] = useState<Record<string, any>>(() => getDefaultConfig('llamacpp', 'llm', true));
  const [fileFilters, setFileFilters] = useState<string[]>([]);

  const [roles, setRoles] = useState<string[]>(['inference']);
  const [gpuType, setGpuType] = useState<string>('');
  const [hostAllow, setHostAllow] = useState<string[]>([]);
  const [hostDeny, setHostDeny] = useState<string[]>([]);
  const [vramGb, setVramGb] = useState<string>('');
  const [ramGb, setRamGb] = useState<string>('');
  const [metadataRows, setMetadataRows] = useState<MetadataRow[]>([]);

  const [hosts, setHosts] = useState<Host[]>([]);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [serverError, setServerError] = useState<{
    message: string;
    errors?: Array<{ field: string; message: string }>;
  } | null>(null);

  // Escape closes the modal
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Live host data for the placement pickers (distinct gpu_type values;
  // host allow/deny chip lists keyed by host.id, labeled host.name)
  useEffect(() => {
    let cancelled = false;
    solarClient
      .getHosts()
      .then((data) => {
        if (!cancelled) setHosts(data);
      })
      .catch(() => {
        /* placement pickers degrade to empty */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const gpuTypes = useMemo(() => {
    const values = new Set<string>();
    for (const host of hosts) {
      if (host.gpu_type) values.add(host.gpu_type);
    }
    return [...values].sort();
  }, [hosts]);

  const roleOptions = useMemo(() => {
    const values = new Set<string>(['inference']);
    for (const host of hosts) {
      for (const role of host.roles ?? []) values.add(role);
    }
    return [...values].sort();
  }, [hosts]);

  const toggleInArray = (arr: string[], value: string): string[] =>
    arr.includes(value) ? arr.filter((v) => v !== value) : [...arr, value];

  const filtersApply = isHuggingFaceSource(modelSource);

  const handleFilterAdd = (pattern = '') => setFileFilters((prev) => [...prev, pattern]);
  const handleFilterChange = (index: number, pattern: string) =>
    setFileFilters((prev) => prev.map((row, i) => (i === index ? pattern : row)));
  const handleFilterRemove = (index: number) => setFileFilters((prev) => prev.filter((_, i) => i !== index));

  const handleMetadataAdd = () => setMetadataRows((prev) => [...prev, { key: '', value: '' }]);
  const handleMetadataChange = (index: number, patch: Partial<MetadataRow>) =>
    setMetadataRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const handleMetadataRemove = (index: number) => setMetadataRows((prev) => prev.filter((_, i) => i !== index));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setServerError(null);

    const metadata: Record<string, string> = {};
    for (const row of metadataRows) {
      if (row.key.trim()) metadata[row.key.trim()] = row.value;
    }

    // Filters only reach the server for huggingface:// sources — nothing else
    // can restrict which files are downloaded.
    const patterns = filtersApply ? fileFilters.map((p) => p.trim()).filter(Boolean) : [];
    const submittedBackend = { ...backend, file_filters: patterns };

    const request: IntentCreateRequest = {
      alias: alias.trim(),
      model_source: modelSource.trim(),
      replicas,
      priority,
      strategy,
      backend: submittedBackend,
      placement: {
        roles,
        gpu_type: gpuType || null,
        host_allow: hostAllow,
        host_deny: hostDeny,
      },
      resources: {
        vram_gb: vramGb === '' ? null : Number(vramGb),
        ram_gb: ramGb === '' ? null : Number(ramGb),
      },
      metadata,
    };

    const errors = validateIntentRequest(request);
    if (errors.length > 0) {
      const grouped: Record<string, string> = {};
      for (const err of errors) {
        if (!grouped[err.field]) grouped[err.field] = err.message;
      }
      setFieldErrors(grouped);
      return;
    }

    setLoading(true);
    try {
      const created = await solarClient.createIntent({
        ...request,
        backend: sanitizeIntentBackend(stripEmptyOptionalFields(submittedBackend)),
      });
      onCreated(created);
    } catch (err: any) {
      console.error('Failed to create intent:', err);
      const detail = extractApiError(err);
      if (err?.response?.status === 409) {
        setServerError({ message: `Alias already claimed by an active intent — "${detail.message}"` });
      } else {
        setServerError({ message: detail.message, errors: detail.errors });
      }
    } finally {
      setLoading(false);
    }
  };

  const fieldError = (field: string) =>
    fieldErrors[field] ? <p className="text-xs text-nord-11 mt-1">{fieldErrors[field]}</p> : null;

  const inputClass =
    'w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent';
  const selectClass =
    'w-full px-3 py-2 bg-nord-2 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent';

  return (
    <div className="fixed inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50 p-4">
      <div className="bg-nord-1 rounded-lg shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto border border-nord-3">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-nord-3 sticky top-0 bg-nord-1 z-10">
          <h2 className="text-xl font-bold text-nord-6">New Intent</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-6">
          {serverError && (
            <div className="rounded-md border border-nord-11 bg-nord-11 bg-opacity-10 p-3">
              <p className="text-sm font-medium text-nord-11">{serverError.message}</p>
              {serverError.errors && serverError.errors.length > 0 && (
                <ul className="mt-2 space-y-1 text-xs text-nord-11">
                  {serverError.errors.map((err, i) => (
                    <li key={i}>
                      <code className="text-nord-4">{err.field}</code>: {err.message}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* Section 1: Deployment */}
          <div>
            <h3 className="text-xs font-semibold text-nord-4 uppercase tracking-wide mb-3">Deployment</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-nord-4 mb-1">
                  Alias <span className="text-nord-11">*</span>
                </label>
                <input
                  type="text"
                  value={alias}
                  onChange={(e) => {
                    setAlias(e.target.value);
                    setFieldErrors((prev) => {
                      const next = { ...prev };
                      delete next.alias;
                      return next;
                    });
                  }}
                  placeholder="model-name:size"
                  className={inputClass}
                />
                <p className="text-xs text-nord-4 mt-1">Served model name — the deployment identity.</p>
                {fieldError('alias')}
              </div>

              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-nord-4 mb-1">
                  Model source <span className="text-nord-11">*</span>
                </label>
                <input
                  type="text"
                  value={modelSource}
                  onChange={(e) => {
                    setModelSource(e.target.value);
                    setFieldErrors((prev) => {
                      const next = { ...prev };
                      delete next.model_source;
                      return next;
                    });
                  }}
                  placeholder="repo://model-name:v1"
                  className={inputClass}
                />
                <p className="text-xs text-nord-4 mt-1">
                  URI scheme: <code>repo://</code> (Harbor), <code>huggingface://</code> (Hub), <code>local://</code>{' '}
                  (already on host).
                </p>
                {fieldError('model_source')}
              </div>

              {filtersApply && (
                <div className="md:col-span-2">
                  <div className="flex items-center justify-between mb-1">
                    <label className="block text-sm font-medium text-nord-4">Download filters (optional)</label>
                    <button
                      type="button"
                      onClick={() => handleFilterAdd()}
                      className="flex items-center gap-1 px-2 py-1 rounded text-xs text-nord-10 hover:bg-nord-2 transition-colors"
                    >
                      <Plus size={14} /> Add filter
                    </button>
                  </div>
                  <p className="text-xs text-nord-4 mb-2">
                    Download only the matching files instead of the whole repository — useful for repos that ship many
                    quantizations. One pattern per row, <code>*</code> allowed.
                  </p>
                  {fileFilters.length === 0 ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-nord-4">No filters — the full repository is downloaded. Try:</span>
                      {FILTER_SUGGESTIONS.map((suggestion) => (
                        <button
                          key={suggestion}
                          type="button"
                          onClick={() => handleFilterAdd(suggestion)}
                          className="px-2 py-1 rounded-full text-xs font-mono border border-nord-3 bg-nord-2 text-nord-4 hover:border-nord-10 hover:text-nord-10 transition-colors"
                        >
                          + {suggestion}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {fileFilters.map((pattern, index) => (
                        <div key={index} className="flex items-center gap-2">
                          <input
                            type="text"
                            value={pattern}
                            onChange={(e) => handleFilterChange(index, e.target.value)}
                            placeholder="*UD-Q4_K_XL*"
                            className={`${inputClass} font-mono text-sm`}
                          />
                          <button
                            type="button"
                            onClick={() => handleFilterRemove(index)}
                            className="p-2 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-11 transition-colors"
                            title="Remove filter"
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  {fieldError('backend.file_filters')}
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Replicas</label>
                <input
                  type="number"
                  value={replicas}
                  onChange={(e) => {
                    setReplicas(e.target.value === '' ? 0 : parseInt(e.target.value, 10));
                    setFieldErrors((prev) => {
                      const next = { ...prev };
                      delete next.replicas;
                      return next;
                    });
                  }}
                  min="0"
                  className={inputClass}
                />
                <p className="text-xs text-nord-4 mt-1">One replica = one host (one-replica-per-host rule).</p>
                {fieldError('replicas')}
              </div>

              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">Priority</label>
                <select
                  value={priority}
                  onChange={(e) => setPriority(e.target.value as IntentPriority)}
                  className={selectClass}
                >
                  <option value="production">production</option>
                  <option value="staging">staging</option>
                  <option value="ephemeral">ephemeral</option>
                </select>
                <p className="text-xs text-nord-4 mt-1">{PRIORITY_EXPLANATIONS[priority]}</p>
              </div>

              <div className="md:col-span-2">
                <label className="block text-sm font-medium text-nord-4 mb-1">Strategy</label>
                <select
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value as IntentStrategy)}
                  className={selectClass}
                >
                  <option value="rolling">rolling — zero-downtime updates (preferred for production)</option>
                  <option value="immediate">immediate — fast replacement, causes a serving gap</option>
                </select>
                <p className="text-xs text-nord-4 mt-1">
                  Governs how replicas are replaced when the deployment changes.
                </p>
              </div>
            </div>
          </div>

          {/* Section 2: Backend */}
          <div>
            <h3 className="text-xs font-semibold text-nord-4 uppercase tracking-wide mb-3">Backend</h3>
            <BackendConfigFields value={backend} onChange={setBackend} forIntent />
            {fieldError('backend')}
            {fieldError('backend.model_file')}
          </div>

          {/* Section 3: Placement (optional) */}
          <details className="group border border-nord-3 rounded-md">
            <summary className="flex items-center justify-between px-4 py-3 cursor-pointer select-none list-none">
              <span className="text-sm font-medium text-nord-4">Placement (optional)</span>
              <ChevronDown size={16} className="text-nord-4 transition-transform group-open:rotate-180" />
            </summary>
            <div className="px-4 pb-4 space-y-4">
              <div>
                <label className="block text-sm font-medium text-nord-4 mb-2">Roles</label>
                <div className="flex flex-wrap gap-2">
                  {roleOptions.map((role) => {
                    const selected = roles.includes(role);
                    return (
                      <button
                        key={role}
                        type="button"
                        onClick={() => setRoles((prev) => toggleInArray(prev, role))}
                        className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                          selected
                            ? 'bg-nord-10 text-nord-6 border-nord-10'
                            : 'bg-nord-2 text-nord-4 border-nord-3 hover:border-nord-4'
                        }`}
                      >
                        {role}
                      </button>
                    );
                  })}
                </div>
                <p className="text-xs text-nord-4 mt-1">Host must have all selected roles.</p>
                {fieldError('placement.roles')}
              </div>

              <div>
                <label className="block text-sm font-medium text-nord-4 mb-1">GPU type</label>
                <select value={gpuType} onChange={(e) => setGpuType(e.target.value)} className={selectClass}>
                  <option value="">Any</option>
                  {gpuTypes.map((g) => (
                    <option key={g} value={g}>
                      {g}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-nord-4 mb-2">Allow hosts</label>
                {hosts.length === 0 ? (
                  <p className="text-xs text-nord-4">No hosts available</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {hosts.map((host) => {
                      const selected = hostAllow.includes(host.id);
                      return (
                        <button
                          key={host.id}
                          type="button"
                          onClick={() => setHostAllow((prev) => toggleInArray(prev, host.id))}
                          className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                            selected
                              ? 'bg-nord-14 text-nord-0 border-nord-14'
                              : 'bg-nord-2 text-nord-4 border-nord-3 hover:border-nord-4'
                          }`}
                        >
                          {host.name}
                        </button>
                      );
                    })}
                  </div>
                )}
                <p className="text-xs text-nord-4 mt-1">If any are selected, placement is restricted to them.</p>
              </div>

              <div>
                <label className="block text-sm font-medium text-nord-4 mb-2">Deny hosts</label>
                {hosts.length === 0 ? (
                  <p className="text-xs text-nord-4">No hosts available</p>
                ) : (
                  <div className="flex flex-wrap gap-2">
                    {hosts.map((host) => {
                      const selected = hostDeny.includes(host.id);
                      return (
                        <button
                          key={host.id}
                          type="button"
                          onClick={() => setHostDeny((prev) => toggleInArray(prev, host.id))}
                          className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                            selected
                              ? 'bg-nord-11 text-nord-6 border-nord-11'
                              : 'bg-nord-2 text-nord-4 border-nord-3 hover:border-nord-4'
                          }`}
                        >
                          {host.name}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          </details>

          {/* Section 4: Resources & Metadata (optional) */}
          <details className="group border border-nord-3 rounded-md">
            <summary className="flex items-center justify-between px-4 py-3 cursor-pointer select-none list-none">
              <span className="text-sm font-medium text-nord-4">Resources &amp; Metadata (optional)</span>
              <ChevronDown size={16} className="text-nord-4 transition-transform group-open:rotate-180" />
            </summary>
            <div className="px-4 pb-4 space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-nord-4 mb-1">VRAM (GB)</label>
                  <input
                    type="number"
                    value={vramGb}
                    onChange={(e) => setVramGb(e.target.value)}
                    min="0"
                    step="0.5"
                    placeholder="Estimated VRAM per replica"
                    className={inputClass}
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-nord-4 mb-1">RAM (GB)</label>
                  <input
                    type="number"
                    value={ramGb}
                    onChange={(e) => setRamGb(e.target.value)}
                    min="0"
                    step="0.5"
                    placeholder="Estimated system RAM"
                    className={inputClass}
                  />
                </div>
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-sm font-medium text-nord-4">Metadata</label>
                  <button
                    type="button"
                    onClick={handleMetadataAdd}
                    className="flex items-center gap-1 px-2 py-1 rounded text-xs text-nord-10 hover:bg-nord-2 transition-colors"
                  >
                    <Plus size={14} /> Add
                  </button>
                </div>
                {metadataRows.length === 0 ? (
                  <p className="text-xs text-nord-4">No metadata — free-form labels, never interpreted.</p>
                ) : (
                  <div className="space-y-2">
                    {metadataRows.map((row, index) => (
                      <div key={index} className="flex items-center gap-2">
                        <input
                          type="text"
                          value={row.key}
                          onChange={(e) => handleMetadataChange(index, { key: e.target.value })}
                          placeholder="key"
                          className={`${inputClass} font-mono text-sm`}
                        />
                        <input
                          type="text"
                          value={row.value}
                          onChange={(e) => handleMetadataChange(index, { value: e.target.value })}
                          placeholder="value"
                          className={`${inputClass} font-mono text-sm`}
                        />
                        <button
                          type="button"
                          onClick={() => handleMetadataRemove(index)}
                          className="p-2 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-11 transition-colors"
                          title="Remove"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </details>

          {/* Actions */}
          <div className="flex gap-3 pt-4 border-t border-nord-3">
            <button
              type="button"
              onClick={onClose}
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-3 text-nord-6 rounded-md hover:bg-nord-2 transition-colors disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={loading}
              className="flex-1 px-4 py-2 bg-nord-10 text-nord-6 rounded-md hover:bg-nord-9 transition-colors disabled:opacity-50 font-medium"
            >
              {loading ? 'Submitting...' : 'Submit Intent'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
