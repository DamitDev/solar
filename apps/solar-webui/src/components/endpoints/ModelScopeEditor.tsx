import { useMemo, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight, Globe2, ListChecks, Plus, Search, Wand2, X } from 'lucide-react';
import { matchingGlob } from './modelScope';

interface ModelScopeEditorProps {
  serveAll: boolean;
  onServeAllChange: (value: boolean) => void;
  /** Exact registry aliases the user ticked. */
  selected: string[];
  onSelectedChange: (value: string[]) => void;
  /** Wildcard rules, kept separate so typing never reshuffles the pick list. */
  globs: string[];
  onGlobsChange: (value: string[]) => void;
  /** Aliases the scope currently grants, resolved by the control plane. */
  matched: string[];
  /** Every registry alias with a live instance. */
  available: string[];
  /** False until the first preview lands; nothing can be classified before that. */
  ready: boolean;
}

export function ModelScopeEditor({
  serveAll,
  onServeAllChange,
  selected,
  onSelectedChange,
  globs,
  onGlobsChange,
  matched,
  available,
  ready,
}: ModelScopeEditorProps) {
  const [search, setSearch] = useState('');
  const [showRules, setShowRules] = useState(globs.length > 0);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const matchedSet = useMemo(() => new Set(matched), [matched]);
  const availableSet = useMemo(() => new Set(available), [available]);

  // A ticked alias whose model is currently unloaded is not in the registry
  // list, but it must stay visible or saving the form would silently drop it.
  const offlineSelected = useMemo(() => selected.filter((alias) => !availableSet.has(alias)), [selected, availableSet]);

  const rows = useMemo(() => {
    const term = search.trim().toLowerCase();
    return [...available, ...offlineSelected]
      .filter((alias) => term === '' || alias.toLowerCase().includes(term))
      .map((alias) => ({
        alias,
        offline: ready && !availableSet.has(alias),
        ticked: selectedSet.has(alias),
        rule: selectedSet.has(alias) ? null : matchedSet.has(alias) ? matchingGlob(alias, globs) : null,
        byRule: !selectedSet.has(alias) && matchedSet.has(alias),
      }));
  }, [available, offlineSelected, availableSet, selectedSet, matchedSet, globs, search, ready]);

  const toggle = (alias: string) => {
    onSelectedChange(selectedSet.has(alias) ? selected.filter((a) => a !== alias) : [...selected, alias]);
  };

  const grantedCount = serveAll ? available.length : matched.length;
  // Only trust an empty result once the registry has actually been resolved,
  // otherwise every open of a scoped endpoint flashes a false alarm.
  const servesNothing = ready && !serveAll && grantedCount === 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <ListChecks size={16} className="text-nord-4" />
        <span className="text-sm font-medium text-nord-4">Model access</span>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => onServeAllChange(true)}
          aria-pressed={serveAll}
          className={`text-left p-3 rounded-md border transition-colors ${
            serveAll ? 'border-nord-10 bg-nord-10 bg-opacity-10' : 'border-nord-3 bg-nord-2 hover:border-nord-4'
          }`}
        >
          <span className="flex items-center gap-2 text-sm font-medium text-nord-6">
            <Globe2 size={15} className={serveAll ? 'text-nord-10' : 'text-nord-4'} />
            All models
          </span>
          <span className="block text-xs text-nord-4 mt-1">
            Everything in the registry, including models added later.
          </span>
        </button>
        <button
          type="button"
          onClick={() => onServeAllChange(false)}
          aria-pressed={!serveAll}
          className={`text-left p-3 rounded-md border transition-colors ${
            !serveAll ? 'border-nord-10 bg-nord-10 bg-opacity-10' : 'border-nord-3 bg-nord-2 hover:border-nord-4'
          }`}
        >
          <span className="flex items-center gap-2 text-sm font-medium text-nord-6">
            <ListChecks size={15} className={!serveAll ? 'text-nord-10' : 'text-nord-4'} />
            Selected models only
          </span>
          <span className="block text-xs text-nord-4 mt-1">Pick the models this endpoint may serve.</span>
        </button>
      </div>

      {serveAll ? (
        <p className="text-xs text-nord-4">
          Keys on this endpoint reach all <span className="text-nord-13">{available.length}</span> registered model
          {available.length === 1 ? '' : 's'}.
        </p>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-nord-4" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter models"
                aria-label="Filter models"
                className="w-full pl-8 pr-3 py-1.5 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-nord-10"
              />
            </div>
            <button
              type="button"
              onClick={() => onSelectedChange(Array.from(new Set([...selected, ...available])))}
              className="px-2.5 py-1.5 rounded-md bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors text-xs"
            >
              Select all
            </button>
            <button
              type="button"
              onClick={() => onSelectedChange([])}
              className="px-2.5 py-1.5 rounded-md bg-nord-3 text-nord-4 hover:text-nord-6 transition-colors text-xs"
            >
              Clear
            </button>
          </div>

          <div className="border border-nord-3 rounded-md divide-y divide-nord-3 max-h-56 overflow-y-auto">
            {rows.length === 0 ? (
              <p className="p-3 text-xs text-nord-4">
                {!ready
                  ? 'Loading registered models…'
                  : available.length === 0
                    ? 'No models are registered right now. Add a wildcard rule below to authorise models before they come online.'
                    : 'No model matches this filter.'}
              </p>
            ) : (
              rows.map(({ alias, offline, ticked, rule, byRule }) => (
                <label
                  key={alias}
                  className={`flex items-center gap-2.5 px-3 py-2 text-sm ${
                    byRule ? 'cursor-default' : 'cursor-pointer hover:bg-nord-2'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={ticked || byRule}
                    disabled={byRule}
                    onChange={() => toggle(alias)}
                    className="accent-nord-10"
                  />
                  <code className={`font-mono flex-1 truncate ${byRule || ticked ? 'text-nord-6' : 'text-nord-5'}`}>
                    {alias}
                  </code>
                  {byRule && (
                    <span className="px-1.5 py-0.5 rounded bg-nord-14 bg-opacity-20 text-nord-14 text-xs shrink-0">
                      {rule ? `via ${rule}` : 'via rule'}
                    </span>
                  )}
                  {offline && (
                    <span
                      className="px-1.5 py-0.5 rounded bg-nord-3 text-nord-4 text-xs shrink-0"
                      title="Not in the registry right now"
                    >
                      not loaded
                    </span>
                  )}
                </label>
              ))
            )}
          </div>

          <div>
            <button
              type="button"
              onClick={() => setShowRules(!showRules)}
              className="flex items-center gap-1.5 text-xs text-nord-4 hover:text-nord-6 transition-colors"
            >
              {showRules ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              <Wand2 size={13} />
              Wildcard rules{globs.length > 0 ? ` (${globs.length})` : ''}
            </button>
            {showRules && (
              <div className="mt-2 space-y-2 pl-1">
                <p className="text-xs text-nord-4">
                  Globs over aliases (<code className="text-nord-5">iris-osl:*</code>). Rules keep matching models that
                  are registered later.
                </p>
                {globs.map((glob, index) => (
                  <div key={index} className="flex gap-2">
                    <input
                      type="text"
                      value={glob}
                      onChange={(e) => onGlobsChange(globs.map((g, i) => (i === index ? e.target.value : g)))}
                      placeholder="e.g. iris-osl:*"
                      aria-label={`Wildcard rule ${index + 1}`}
                      className="flex-1 px-3 py-1.5 bg-nord-2 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:outline-none focus:ring-2 focus:ring-nord-10 font-mono text-sm"
                    />
                    <button
                      type="button"
                      onClick={() => onGlobsChange(globs.filter((_, i) => i !== index))}
                      title="Remove rule"
                      className="px-2 rounded-md bg-nord-3 text-nord-4 hover:bg-nord-11 hover:bg-opacity-20 hover:text-nord-11 transition-colors"
                    >
                      <X size={14} />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => onGlobsChange([...globs, ''])}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-dashed border-nord-3 text-nord-4 hover:text-nord-6 hover:border-nord-10 transition-colors text-xs"
                >
                  <Plus size={13} />
                  Add rule
                </button>
              </div>
            )}
          </div>

          <div
            className={`p-3 rounded-md border text-xs ${
              servesNothing ? 'border-nord-12 bg-nord-12 bg-opacity-10' : 'border-nord-3 bg-nord-2'
            }`}
          >
            {!ready ? (
              <p className="text-nord-4">Resolving the scope against the registry…</p>
            ) : servesNothing ? (
              <p className="flex items-start gap-2 text-nord-12">
                <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                Nothing is selected, so every request to this endpoint returns <code>model_not_found</code>.
              </p>
            ) : (
              <p className="text-nord-4">
                Grants <span className="text-nord-13">{grantedCount}</span> of {available.length} registered model
                {available.length === 1 ? '' : 's'}. Anything else returns <code>model_not_found</code>.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
