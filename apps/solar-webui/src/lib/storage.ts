import { HostStorage, StoredModel, StoredModelOrigin } from '@/api/types';

/**
 * Pure helpers for the Storage page — kept outside React so the selection
 * and filtering logic is unit-testable without rendering.
 */

export interface StorageFilters {
  usage: 'all' | 'unused' | 'in_use';
  origin: 'all' | StoredModelOrigin;
  /** Minimum size in bytes; 0 means any size. */
  minSizeBytes: number;
}

export const DEFAULT_STORAGE_FILTERS: StorageFilters = {
  usage: 'all',
  origin: 'all',
  minSizeBytes: 0,
};

export const MIN_SIZE_OPTIONS: Array<{ label: string; bytes: number }> = [
  { label: 'Any size', bytes: 0 },
  { label: '> 1 GB', bytes: 1024 ** 3 },
  { label: '> 10 GB', bytes: 10 * 1024 ** 3 },
  { label: '> 50 GB', bytes: 50 * 1024 ** 3 },
];

/** Selection key: stable across refetches, view switches, and pagination. */
export function selectionKey(hostId: string, slug: string): string {
  return `${hostId}::${slug}`;
}

/** True when any active instance is using this model copy. */
export function isModelInUse(model: StoredModel): boolean {
  return model.in_use_by.length > 0;
}

/** Sum of sizes of all models that are not in use anywhere (reclaimable). */
export function reclaimableBytes(hosts: HostStorage[]): number {
  return hosts.reduce(
    (sum, host) => sum + host.models.reduce((s, m) => s + (isModelInUse(m) ? 0 : m.size_bytes), 0),
    0,
  );
}

/** Sum of every model copy on every host. */
export function totalStoredBytes(hosts: HostStorage[]): number {
  return hosts.reduce((sum, host) => sum + host.total_size_bytes, 0);
}

/** Distinct model count across all hosts (grouped by display name). */
export function distinctModelCount(hosts: HostStorage[]): number {
  const keys = new Set<string>();
  for (const host of hosts) {
    for (const model of host.models) {
      keys.add(model.model_name ?? model.slug);
    }
  }
  return keys.size;
}

/** Count of reachable hosts (unreachable ones are excluded). */
export function reachableHostCount(hosts: HostStorage[]): number {
  return hosts.filter((h) => h.reachable).length;
}

export function originLabel(origin: StoredModelOrigin): string {
  switch (origin) {
    case 'repository':
      return 'Repository';
    case 'huggingface':
      return 'HuggingFace';
    case 'local':
      return 'Local';
    default:
      return 'Unknown';
  }
}

/** Badge classes for the origin column (mirrors getStatusColor style). */
export function originBadgeClass(origin: StoredModelOrigin): string {
  switch (origin) {
    case 'repository':
      return 'bg-nord-10/20 text-nord-8';
    case 'huggingface':
      return 'bg-nord-15/20 text-nord-15';
    case 'local':
      return 'bg-nord-3 text-nord-4';
    default:
      return 'bg-nord-3 text-nord-4 italic';
  }
}

/** Does one model copy pass every active filter? */
export function modelPassesFilters(model: StoredModel, filters: StorageFilters): boolean {
  if (filters.usage === 'unused' && isModelInUse(model)) return false;
  if (filters.usage === 'in_use' && !isModelInUse(model)) return false;
  if (filters.origin !== 'all' && model.origin !== filters.origin) return false;
  if (filters.minSizeBytes > 0 && model.size_bytes < filters.minSizeBytes) return false;
  return true;
}

/**
 * Filter the model lists in place. Unreachable hosts are always kept so
 * their panel stays visible with the "unreachable" pill; reachable hosts
 * are dropped only when every model was filtered out *and* a filter is
 * active (a genuinely empty host still shows its "No models" body).
 */
export function applyStorageFilters(hosts: HostStorage[], filters: StorageFilters): HostStorage[] {
  const filtersActive = filters.usage !== 'all' || filters.origin !== 'all' || filters.minSizeBytes > 0;
  return hosts
    .map((host) => ({
      ...host,
      models: host.models.filter((m) => modelPassesFilters(m, filters)),
    }))
    .filter((host) => !host.reachable || host.models.length > 0 || !filtersActive);
}

/** Does a host or one of its models match the search query? */
export function storageSearchMatches(host: HostStorage, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  if (host.host_name.toLowerCase().includes(q)) return true;
  return host.models.some((m) => (m.model_name ?? '').toLowerCase().includes(q) || m.slug.toLowerCase().includes(q));
}

export interface StoredModelCopy {
  hostId: string;
  hostName: string;
  model: StoredModel;
}

export interface ModelGroup {
  /** Group key: model_name ?? slug. */
  key: string;
  /** Display name. */
  modelName: string;
  origin: StoredModelOrigin;
  /** Versions of every copy (may disagree across hosts). */
  versions: Array<string | null>;
  copies: StoredModelCopy[];
}

/** Merge the same model across hosts into one group (By model view). */
export function groupByModel(hosts: HostStorage[]): ModelGroup[] {
  const groups = new Map<string, ModelGroup>();
  for (const host of hosts) {
    for (const model of host.models) {
      const name = model.model_name ?? model.slug;
      let group = groups.get(name);
      if (!group) {
        group = { key: name, modelName: name, origin: model.origin, versions: [], copies: [] };
        groups.set(name, group);
      }
      group.versions.push(model.version);
      group.copies.push({ hostId: host.host_id, hostName: host.host_name, model });
    }
  }
  return [...groups.values()];
}

/** Aggregate size of every copy in the group. */
export function groupSizeBytes(group: ModelGroup): number {
  return group.copies.reduce((s, c) => s + c.model.size_bytes, 0);
}

/** Number of hosts whose copy of this model is currently in use. */
export function groupInUseHostCount(group: ModelGroup): number {
  return group.copies.filter((c) => isModelInUse(c.model)).length;
}

/** True when copies disagree on version (mixed versions hint). */
export function groupHasMixedVersions(group: ModelGroup): boolean {
  const nonNull = new Set(group.versions.filter((v): v is string => v != null));
  return nonNull.size > 1;
}

/** Deletable (not in use) copies of the group. */
export function groupIdleCopies(group: ModelGroup): StoredModelCopy[] {
  return group.copies.filter((c) => !isModelInUse(c.model));
}
