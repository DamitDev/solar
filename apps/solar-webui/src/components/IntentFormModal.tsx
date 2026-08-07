/**
 * IntentFormModal — declarative intent form, for both submission (U-003) and
 * editing (U-006).
 *
 * Fields per spec deployment-intent.md §4.1: alias, model_source, replicas,
 * priority, strategy, backend (shared editor), placement, resources, metadata.
 * Client-side shape rules (§4.7) run before submit; the server stays
 * authoritative. Server errors surface as a red alert: 409 = alias conflict,
 * 422 = structured errors[] list.
 *
 * Passing `intent` switches to edit mode: every field is seeded from that
 * intent and the form PUTs to §12.5, which has full-replace semantics — so the
 * form must send a complete spec, and anything it fails to hydrate would be
 * silently reset to a default.
 */

import { useEffect, useMemo, useState } from 'react';
import { X, Plus, Trash2, ChevronDown, AlertTriangle } from 'lucide-react';
import solarClient from '@/api/client';
import { Host, Intent, IntentCreateRequest, IntentPriority, IntentStrategy } from '@/api/types';
import { extractApiError } from '@/lib/apiErrors';
import { cn } from '@/lib/utils';
import { validateIntentRequest, sanitizeIntentBackend } from '@/lib/intentValidation';
import { getDefaultConfig, stripEmptyOptionalFields } from '@/lib/backendConfig';
import { BackendConfigFields } from './BackendConfigFields';

interface IntentFormModalProps {
  /** Edit mode: seed from this intent and update it instead of creating one. */
  intent?: Intent;
  initial?: Partial<IntentCreateRequest>;
  onClose: () => void;
  onSaved: (intent: Intent) => void;
}

interface MetadataRow {
  key: string;
  value: string;
}

const numberToInput = (value: number | null | undefined): string => (value == null ? '' : String(value));

/** Either seed: a stored intent widens priority/strategy to plain strings. */
type IntentSeed = Omit<Partial<IntentCreateRequest>, 'priority' | 'strategy'> & {
  priority?: string;
  strategy?: string;
};

/** Ready-made patterns for the most common filtered HuggingFace pulls. */
const FILTER_SUGGESTIONS = ['*UD-Q4_K_XL*', '*Q8_0*', 'mmproj-BF16.gguf', '*.safetensors', 'tokenizer*'];

const isHuggingFaceSource = (source: string) => source.trim().startsWith('huggingface://');

const PRIORITY_EXPLANATIONS: Record<IntentPriority, string> = {
  production: 'Never displaced automatically; may displace lower priorities.',
  staging: 'May be displaced by production; migrates when possible.',
  ephemeral: 'Lowest priority — first to be stopped or migrated when capacity is needed.',
};

/**
 * Fields with an inline error slot. The banner lists the rest, so an error is
 * shown exactly once — next to its input where it can be fixed, or in the
 * banner when there is no input to attach it to.
 */
const INLINE_ERROR_FIELDS: ReadonlySet<string> = new Set([
  'alias',
  'model_source',
  'replicas',
  'backend',
  'backend.file_filters',
  'backend.model_file',
  'backend.mmproj',
  'backend.chat_template_kwargs',
  'backend.device',
  'placement.roles',
  'placement.gpu_type',
  'placement.host_allow',
  'resources.vram_gb',
  'resources.ram_gb',
]);

export function IntentFormModal({ intent, initial, onClose, onSaved }: IntentFormModalProps) {
  const editing = intent != null;
  // In edit mode the intent is the source of truth for every field; `initial`
  // only ever pre-fills a new intent.
  const seed: IntentSeed | undefined = intent ?? initial;

  const [loading, setLoading] = useState(false);
  const [alias, setAlias] = useState(seed?.alias ?? '');
  const [modelSource, setModelSource] = useState(seed?.model_source ?? '');
  const [replicas, setReplicas] = useState<number>(seed?.replicas ?? 1);
  const [priority, setPriority] = useState<IntentPriority>((seed?.priority as IntentPriority) ?? 'production');
  const [strategy, setStrategy] = useState<IntentStrategy>((seed?.strategy as IntentStrategy) ?? 'rolling');
  const [backend, setBackend] = useState<Record<string, any>>(() =>
    seed?.backend ? { ...seed.backend } : getDefaultConfig('llamacpp', 'llm', true),
  );
  const [fileFilters, setFileFilters] = useState<string[]>(() => seed?.backend?.file_filters ?? []);

  const [roles, setRoles] = useState<string[]>(seed?.placement?.roles ?? ['inference']);
  const [gpuType, setGpuType] = useState<string>(seed?.placement?.gpu_type ?? '');
  const [hostAllow, setHostAllow] = useState<string[]>(seed?.placement?.host_allow ?? []);
  const [hostDeny, setHostDeny] = useState<string[]>(seed?.placement?.host_deny ?? []);
  const [vramGb, setVramGb] = useState<string>(numberToInput(seed?.resources?.vram_gb));
  const [ramGb, setRamGb] = useState<string>(numberToInput(seed?.resources?.ram_gb));
  const [metadataRows, setMetadataRows] = useState<MetadataRow[]>(() =>
    Object.entries(seed?.metadata ?? {}).map(([key, value]) => ({ key, value })),
  );

  // A rollout in flight is abandoned and re-planned against the saved spec
  // (§11.5.1), so saying so beats letting the operator discover it.
  const rolloutInFlight = intent?.status?.strategy_progress != null;

  // Collapsed optional sections would hide seeded constraints the operator is
  // about to re-submit, so open them whenever they hold anything.
  const [placementOpen, setPlacementOpen] = useState(
    () => gpuType !== '' || hostAllow.length > 0 || hostDeny.length > 0 || roles.join() !== 'inference',
  );
  const [extrasOpen, setExtrasOpen] = useState(() => vramGb !== '' || ramGb !== '' || metadataRows.length > 0);

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
    // Errors from the previous attempt are about the previous values; keeping
    // them would leave a fixed field marked invalid until it happened to fail
    // again.
    setFieldErrors({});

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
      const payload = {
        ...request,
        backend: sanitizeIntentBackend(stripEmptyOptionalFields(submittedBackend)),
      };
      const saved = editing
        ? await solarClient.updateIntent(intent!.id, payload)
        : await solarClient.createIntent(payload);
      onSaved(saved);
    } catch (err: any) {
      console.error(editing ? 'Failed to update intent:' : 'Failed to create intent:', err);
      const detail = extractApiError(err);
      if (err?.response?.status === 409) {
        setServerError({
          message: editing ? detail.message : `Alias already claimed by an active intent — "${detail.message}"`,
        });
      } else {
        // C3: server validation errors (field-level) also surface inline,
        // next to the client-side ones — the server is authoritative.
        if (detail.errors && detail.errors.length > 0) {
          const grouped: Record<string, string> = {};
          for (const errItem of detail.errors) {
            if (!grouped[errItem.field]) grouped[errItem.field] = errItem.message;
          }
          setFieldErrors((prev) => ({ ...prev, ...grouped }));
        }
        // The banner lists only what has no field to sit next to; an error
        // shown inline and again in the banner reads as two problems.
        setServerError({
          message: detail.message,
          errors: (detail.errors ?? []).filter((errItem) => !INLINE_ERROR_FIELDS.has(errItem.field)),
        });
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
          <h2 className="text-xl font-bold text-nord-6">{editing ? `Edit ${intent!.alias}` : 'New Intent'}</h2>
          <button onClick={onClose} className="p-1 hover:bg-nord-2 rounded transition-colors text-nord-4">
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="p-6 space-y-6">
          {editing && (
            <div className="rounded-md border border-nord-3 bg-nord-2 p-3 space-y-1">
              <p className="text-sm text-nord-4">
                Saving replaces the whole configuration. Replicas are converted using the{' '}
                <span className="font-medium text-nord-6">{strategy}</span> strategy below —{' '}
                {strategy === 'rolling'
                  ? 'one replica at a time, so the alias keeps serving.'
                  : 'all replicas stop before the replacements start, so the alias briefly serves nothing.'}
              </p>
              {rolloutInFlight && (
                <p className="flex items-center gap-2 text-sm text-nord-13">
                  <AlertTriangle size={14} className="shrink-0" />
                  An update is in progress. Saving restarts it with the new configuration.
                </p>
              )}
            </div>
          )}

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
                  disabled={editing}
                  className={cn(inputClass, editing && 'opacity-60 cursor-not-allowed')}
                />
                <p className="text-xs text-nord-4 mt-1">
                  {editing
                    ? 'Served model name — the deployment identity, so it cannot be changed. Serving a different name means a new intent.'
                    : 'Served model name — the deployment identity.'}
                </p>
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
                  disabled={editing}
                  className={cn(inputClass, editing && 'opacity-60 cursor-not-allowed')}
                />
                <p className="text-xs text-nord-4 mt-1">
                  {editing ? (
                    'The model identity is fixed on an existing intent — the hosts cache files by it, so changing it would orphan them. Create a new intent to serve a different source.'
                  ) : (
                    <>
                      URI scheme: <code>repo://</code> (Harbor), <code>huggingface://</code> (Hub),{' '}
                      <code>local://</code> (already on host).
                    </>
                  )}
                </p>
                {fieldError('model_source')}
              </div>

              {filtersApply && (
                <div className="md:col-span-2">
                  <div className="flex items-center justify-between mb-1">
                    <label className="block text-sm font-medium text-nord-4">Download filters (optional)</label>
                    {!editing && (
                      <button
                        type="button"
                        onClick={() => handleFilterAdd()}
                        className="flex items-center gap-1 px-2 py-1 rounded text-xs text-nord-10 hover:bg-nord-2 transition-colors"
                      >
                        <Plus size={14} /> Add filter
                      </button>
                    )}
                  </div>
                  <p className="text-xs text-nord-4 mb-2">
                    {editing
                      ? 'Part of the model identity — changing which files an existing intent downloads would orphan the host cache. Create a new intent for different filters.'
                      : 'Download only the matching files instead of the whole repository — useful for repos that ship many quantizations. One pattern per row, <code>*</code> allowed.'}
                  </p>
                  {fileFilters.length === 0 ? (
                    !editing ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs text-nord-4">
                          No filters — the full repository is downloaded. Try:
                        </span>
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
                      <span className="text-xs text-nord-4">No filters — the full repository is downloaded.</span>
                    )
                  ) : (
                    <div className="space-y-2">
                      {fileFilters.map((pattern, index) => (
                        <div key={index} className="flex items-center gap-2">
                          <input
                            type="text"
                            value={pattern}
                            onChange={(e) => handleFilterChange(index, e.target.value)}
                            placeholder="*UD-Q4_K_XL*"
                            disabled={editing}
                            className={`${inputClass} font-mono text-sm ${editing ? 'opacity-60 cursor-not-allowed' : ''}`}
                          />
                          {!editing && (
                            <button
                              type="button"
                              onClick={() => handleFilterRemove(index)}
                              className="p-2 rounded hover:bg-nord-2 text-nord-4 hover:text-nord-11 transition-colors"
                              title="Remove filter"
                            >
                              <Trash2 size={16} />
                            </button>
                          )}
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
            <BackendConfigFields value={backend} onChange={setBackend} forIntent fieldError={fieldError} />
            {fieldError('backend')}
            {fieldError('backend.model_file')}
            {fieldError('backend.spec_type')}
            {fieldError('backend.spec_draft_model')}
          </div>

          {/* Section 3: Placement (optional) */}
          <details
            open={placementOpen}
            onToggle={(e) => setPlacementOpen((e.currentTarget as HTMLDetailsElement).open)}
            className="group border border-nord-3 rounded-md"
          >
            <summary className="flex items-center justify-between px-4 py-3 cursor-pointer select-none list-none">
              <span className="text-sm font-medium text-nord-4">Host selection (optional)</span>
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
                {fieldError('placement.gpu_type')}
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
                {fieldError('placement.host_allow')}
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
          <details
            open={extrasOpen}
            onToggle={(e) => setExtrasOpen((e.currentTarget as HTMLDetailsElement).open)}
            className="group border border-nord-3 rounded-md"
          >
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
              {loading ? 'Saving...' : editing ? 'Save changes' : 'Submit Intent'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
