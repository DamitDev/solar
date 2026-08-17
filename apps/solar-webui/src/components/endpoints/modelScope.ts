import { useEffect, useState } from 'react';
import solarClient from '@/api/client';

/**
 * Model scoping in solar-control is a flat list of fnmatch globs over registry
 * aliases. For editing we split that list in two: a plain alias is a concrete
 * model the user ticked, anything containing glob metacharacters is a rule that
 * also covers models which are not registered yet.
 */
const GLOB_METACHARACTERS = /[*?[\]]/;

export function isGlobPattern(pattern: string): boolean {
  return GLOB_METACHARACTERS.test(pattern);
}

export interface SplitPatterns {
  /** Exact aliases, edited through the model pick list. */
  selected: string[];
  /** Wildcard rules, edited as free text. */
  globs: string[];
}

export function splitPatterns(patterns: string[] | undefined): SplitPatterns {
  const all = patterns ?? [];
  return {
    selected: all.filter((p) => !isGlobPattern(p)),
    globs: all.filter(isGlobPattern),
  };
}

/** Recombine into the wire format, dropping blank rows left by the glob editor. */
export function combinePatterns({ selected, globs }: SplitPatterns): string[] {
  return [...selected, ...globs].map((p) => p.trim()).filter((p) => p !== '');
}

/**
 * Which wildcard rule covers an alias, for labelling only. The control plane
 * stays authoritative for what actually matches (see `useModelScopePreview`);
 * a disagreement here only costs the rule name in a chip.
 */
export function matchingGlob(alias: string, globs: string[]): string | null {
  for (const glob of globs) {
    const source = glob
      .replace(/[.+^${}()|\\]/g, '\\$&')
      .replace(/\*/g, '.*')
      .replace(/\?/g, '.');
    try {
      if (new RegExp(`^${source}$`).test(alias)) return glob;
    } catch {
      // An unbalanced character class is not a usable label source.
    }
  }
  return null;
}

export interface ModelScopePreview {
  /** Registry aliases the scope currently grants. */
  matched: string[];
  /** Every registry alias with a live instance. */
  available: string[];
  /** False until the first preview response lands. */
  ready: boolean;
}

/**
 * Resolve a scope against the live registry via the control plane, so the form
 * shows the same matches the gateway will enforce instead of reimplementing
 * fnmatch in the browser. Debounced because it runs on every keystroke; the
 * result is advisory, so failures degrade to an empty match list.
 */
export function useModelScopePreview(serveAll: boolean, patterns: string[]): ModelScopePreview {
  const [matched, setMatched] = useState<string[]>([]);
  const [available, setAvailable] = useState<string[]>([]);
  const [ready, setReady] = useState(false);
  const patternKey = patterns.join('\n');

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const data = await solarClient.previewEndpointModels({
          serve_all_models: serveAll,
          model_patterns: patternKey.split('\n').filter((p) => p.trim() !== ''),
        });
        if (cancelled) return;
        setMatched(data.aliases ?? []);
        // Older control planes omit `available`; keep the last known list.
        if (data.available) setAvailable(data.available);
      } catch {
        if (!cancelled) setMatched([]);
      } finally {
        if (!cancelled) setReady(true);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [serveAll, patternKey]);

  return { matched, available, ready };
}
