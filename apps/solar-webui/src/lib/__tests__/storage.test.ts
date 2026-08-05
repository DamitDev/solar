import { describe, expect, it } from 'vitest';
import { HostStorage, StoredModel } from '@/api/types';
import {
  applyStorageFilters,
  DEFAULT_STORAGE_FILTERS,
  distinctModelCount,
  groupByModel,
  groupHasMixedVersions,
  groupInUseHostCount,
  groupSizeBytes,
  isModelInUse,
  reclaimableBytes,
  selectionKey,
  StorageFilters,
  totalStoredBytes,
} from '@/lib/storage';

function model(slug: string, overrides: Partial<StoredModel> = {}): StoredModel {
  return {
    slug,
    model_name: null,
    version: null,
    category: null,
    source_uri: null,
    origin: 'unknown',
    harbor_ref: null,
    path: `/opt/solar/models/${slug}`,
    size_bytes: 1000,
    downloaded_at: null,
    in_use_by: [],
    files: [],
    ...overrides,
  };
}

function host(id: string, models: StoredModel[], overrides: Partial<HostStorage> = {}): HostStorage {
  return {
    host_id: id,
    host_name: `host-${id}`,
    reachable: true,
    error: null,
    disk_total_gb: 100,
    disk_used_gb: 50,
    disk_available_gb: 50,
    total_size_bytes: models.reduce((s, m) => s + m.size_bytes, 0),
    models,
    ...overrides,
  };
}

describe('selectionKey', () => {
  it('round-trips host id and slug', () => {
    const key = selectionKey('host-1', 'repo--iris-osl--v3');
    expect(key).toBe('host-1::repo--iris-osl--v3');
  });
});

describe('reclaimableBytes', () => {
  it('excludes in-use entries', () => {
    const hosts = [
      host('a', [
        model('m1', { size_bytes: 100 }),
        model('m2', { size_bytes: 200, in_use_by: [{ instance_id: 'i1', alias: 'x', status: 'running' }] }),
      ]),
      host('b', [model('m3', { size_bytes: 400 })]),
    ];
    expect(reclaimableBytes(hosts)).toBe(500);
    expect(totalStoredBytes(hosts)).toBe(700);
  });
});

describe('distinctModelCount', () => {
  it('counts the same model on two hosts once', () => {
    const hosts = [
      host('a', [model('m1', { model_name: 'iris' }), model('m2')]),
      host('b', [model('m3', { model_name: 'iris' })]),
    ];
    expect(distinctModelCount(hosts)).toBe(2);
  });
});

describe('isModelInUse', () => {
  it('is true only when in_use_by is non-empty', () => {
    expect(isModelInUse(model('m1'))).toBe(false);
    expect(isModelInUse(model('m1', { in_use_by: [{ instance_id: 'i1', alias: 'x', status: 'starting' }] }))).toBe(
      true,
    );
  });
});

describe('groupByModel', () => {
  it('merges the same model from two hosts and keeps per-host copies', () => {
    const hosts = [
      host('a', [model('m1', { model_name: 'iris', version: 'v1', size_bytes: 100 })]),
      host('b', [model('m1', { model_name: 'iris', version: 'v2', size_bytes: 300 })]),
      host('c', [model('other', { size_bytes: 50 })]),
    ];
    const groups = groupByModel(hosts);
    expect(groups).toHaveLength(2);

    const iris = groups.find((g) => g.key === 'iris')!;
    expect(iris.copies).toHaveLength(2);
    expect(iris.copies.map((c) => c.hostId).sort()).toEqual(['a', 'b']);
    expect(iris.versions).toEqual(['v1', 'v2']);
    expect(groupSizeBytes(iris)).toBe(400);
    expect(groupHasMixedVersions(iris)).toBe(true);
    expect(groupInUseHostCount(iris)).toBe(0);

    const single = groups.find((g) => g.key === 'other')!;
    expect(single.copies).toHaveLength(1);
    expect(groupHasMixedVersions(single)).toBe(false);
  });

  it('groups by slug when model_name is absent', () => {
    const hosts = [host('a', [model('repo--x--v1')]), host('b', [model('repo--x--v1')])];
    const groups = groupByModel(hosts);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe('repo--x--v1');
  });

  it('counts in-use hosts', () => {
    const hosts = [
      host('a', [model('m1', { in_use_by: [{ instance_id: 'i1', alias: 'x', status: 'running' }] })]),
      host('b', [model('m1')]),
    ];
    const groups = groupByModel(hosts);
    expect(groupInUseHostCount(groups[0])).toBe(1);
  });
});

describe('applyStorageFilters', () => {
  const hosts = [
    host('a', [
      model('repo-model', { origin: 'repository', size_bytes: 2 * 1024 ** 3 }),
      model('hf-model', {
        origin: 'huggingface',
        size_bytes: 20 * 1024 ** 3,
        in_use_by: [{ instance_id: 'i1', alias: 'x', status: 'running' }],
      }),
    ]),
    host('b', [model('local-model', { origin: 'local', size_bytes: 500 })], { reachable: false, error: 'down' }),
  ];

  it('passes everything through with default filters (except unreachable hosts keep their panel)', () => {
    const filtered = applyStorageFilters(hosts, DEFAULT_STORAGE_FILTERS);
    expect(filtered).toHaveLength(2);
    const unreachable = filtered.find((h) => !h.reachable)!;
    expect(unreachable.models).toHaveLength(1); // kept so the panel renders
    const reachable = filtered.find((h) => h.reachable)!;
    expect(reachable.models).toHaveLength(2);
  });

  it('filters by usage', () => {
    const unused: StorageFilters = { ...DEFAULT_STORAGE_FILTERS, usage: 'unused' };
    const filtered = applyStorageFilters(hosts, unused);
    const reachable = filtered.find((h) => h.reachable)!;
    expect(reachable.models.map((m) => m.slug)).toEqual(['repo-model']);

    const inUse: StorageFilters = { ...DEFAULT_STORAGE_FILTERS, usage: 'in_use' };
    const inUseFiltered = applyStorageFilters(hosts, inUse);
    const reachableInUse = inUseFiltered.find((h) => h.reachable)!;
    expect(reachableInUse.models.map((m) => m.slug)).toEqual(['hf-model']);
  });

  it('filters by origin', () => {
    const repo: StorageFilters = { ...DEFAULT_STORAGE_FILTERS, origin: 'repository' };
    const filtered = applyStorageFilters(hosts, repo);
    const reachable = filtered.find((h) => h.reachable)!;
    expect(reachable.models.map((m) => m.slug)).toEqual(['repo-model']);
  });

  it('filters by minimum size', () => {
    const big: StorageFilters = { ...DEFAULT_STORAGE_FILTERS, minSizeBytes: 10 * 1024 ** 3 };
    const filtered = applyStorageFilters(hosts, big);
    const reachable = filtered.find((h) => h.reachable)!;
    expect(reachable.models.map((m) => m.slug)).toEqual(['hf-model']);
  });
});
