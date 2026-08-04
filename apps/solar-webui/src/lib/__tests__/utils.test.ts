import { describe, expect, it } from 'vitest';
import {
  formatBytes,
  formatDate,
  formatDiskUsage,
  formatMemoryUsage,
  formatRelativeTime,
  formatTokenCount,
  formatUptime,
  getCatalogStatusColor,
  getGpuTypeBadgeClass,
  getGpuTypeLabel,
  getIntentOwnership,
  getIntentPhaseColor,
  getMemoryColor,
  getRoleBadgeClass,
  getStatusColor,
} from '@/lib/utils';

describe('formatTokenCount', () => {
  it('renders em-dash for missing or invalid counts', () => {
    expect(formatTokenCount(undefined)).toBe('—');
    expect(formatTokenCount(null)).toBe('—');
    expect(formatTokenCount(Number.NaN)).toBe('—');
  });

  it('handles zero', () => {
    expect(formatTokenCount(0)).toBe('0');
  });

  it('formats thousands', () => {
    expect(formatTokenCount(1234)).toBe('1.2K');
    expect(formatTokenCount(999)).toBe('999');
  });

  it('formats millions', () => {
    expect(formatTokenCount(12_345_678)).toBe('12.35M');
  });

  it('formats billions', () => {
    expect(formatTokenCount(1_234_567_890)).toBe('1.23B');
  });

  it('keeps the sign for negative counts', () => {
    expect(formatTokenCount(-1_500)).toBe('-1.5K');
  });

  it('rounds small values without trailing zeros', () => {
    expect(formatTokenCount(42)).toBe('42');
    expect(formatTokenCount(42.5)).toBe('42.5');
  });
});

describe('formatBytes', () => {
  it('renders em-dash for missing or invalid values', () => {
    expect(formatBytes(undefined)).toBe('—');
    expect(formatBytes(null)).toBe('—');
  });

  it('handles zero', () => {
    expect(formatBytes(0)).toBe('0 B');
  });

  it('scales through units', () => {
    expect(formatBytes(500)).toBe('500 B');
    expect(formatBytes(1024)).toBe('1.0 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(3 * 1024 ** 3)).toBe('3.0 GB');
  });
});

describe('formatUptime', () => {
  it('renders Not running for missing start', () => {
    expect(formatUptime(undefined)).toBe('Not running');
  });

  it('formats seconds, minutes, hours, days', () => {
    const now = Date.now();
    expect(formatUptime(new Date(now - 30_000).toISOString())).toBe('30s');
    expect(formatUptime(new Date(now - 65_000).toISOString())).toBe('1m 5s');
    expect(formatUptime(new Date(now - 3_700_000).toISOString())).toBe('1h 1m');
    expect(formatUptime(new Date(now - 90_000_000).toISOString())).toBe('1d 1h');
  });
});

describe('formatRelativeTime', () => {
  it('renders Never for missing dates', () => {
    expect(formatRelativeTime(undefined)).toBe('Never');
  });

  it('renders just now, minutes, hours, days', () => {
    const now = Date.now();
    expect(formatRelativeTime(new Date(now - 5_000).toISOString())).toBe('just now');
    expect(formatRelativeTime(new Date(now - 5 * 60_000).toISOString())).toBe('5m ago');
    expect(formatRelativeTime(new Date(now - 3 * 3_600_000).toISOString())).toBe('3h ago');
    expect(formatRelativeTime(new Date(now - 2 * 86_400_000).toISOString())).toBe('2d ago');
  });

  it('falls back to absolute date past a week', () => {
    const old = new Date(Date.now() - 30 * 86_400_000).toISOString();
    expect(formatRelativeTime(old)).toBe(formatDate(old));
  });
});

describe('formatDate', () => {
  it('renders Never for missing dates', () => {
    expect(formatDate(undefined)).toBe('Never');
  });

  it('formats a valid ISO date', () => {
    const out = formatDate('2026-01-02T03:04:05Z');
    expect(out).toContain('2026');
    expect(out).not.toBe('Never');
  });
});

describe('GPU and role labels', () => {
  it('maps known GPU types and falls back to the raw value', () => {
    expect(getGpuTypeLabel('nvidia_cuda')).toBe('NVIDIA CUDA');
    expect(getGpuTypeLabel('apple_mps')).toBe('Apple MPS');
    expect(getGpuTypeLabel('cpu')).toBe('CPU');
    expect(getGpuTypeLabel('quantum_doohickey')).toBe('quantum_doohickey');
  });

  it('returns badge classes for GPU types', () => {
    expect(getGpuTypeBadgeClass('nvidia_cuda')).toContain('bg-nord-14');
    expect(getGpuTypeBadgeClass('cpu')).toContain('bg-nord-3');
    expect(getGpuTypeBadgeClass('unknown')).toContain('bg-nord-3');
  });

  it('returns role badge classes', () => {
    expect(getRoleBadgeClass('inference')).toContain('text-nord-6');
    expect(getRoleBadgeClass('training')).toContain('bg-nord-15');
    expect(getRoleBadgeClass('weird')).toContain('bg-nord-6');
  });
});

describe('status and memory colors', () => {
  it('maps statuses to color classes', () => {
    expect(getStatusColor('running')).toContain('bg-nord-14');
    expect(getStatusColor('online')).toContain('bg-nord-14');
    expect(getStatusColor('stopped')).toContain('bg-nord-3');
    expect(getStatusColor('starting')).toContain('bg-nord-13');
    expect(getStatusColor('failed')).toContain('bg-nord-11');
    expect(getStatusColor('mystery')).toContain('bg-nord-3');
  });

  it('maps memory percent to green/yellow/red', () => {
    expect(getMemoryColor(50)).toBe('bg-nord-14');
    expect(getMemoryColor(80)).toBe('bg-nord-13');
    expect(getMemoryColor(95)).toBe('bg-nord-11');
  });

  it('formats memory and disk usage strings', () => {
    expect(formatMemoryUsage(3.2, 24, 13.3)).toBe('3.2 / 24.0 GB (13.3%)');
    expect(formatDiskUsage(1.5, 100)).toBe('1.5 / 100.0 GB (1.5%)');
  });
});

describe('catalog and intent phase colors', () => {
  it('maps catalog statuses', () => {
    expect(getCatalogStatusColor('available')).toContain('bg-nord-14');
    expect(getCatalogStatusColor('deployed')).toContain('bg-nord-10');
    expect(getCatalogStatusColor('unknown')).toContain('bg-nord-13');
    expect(getCatalogStatusColor('anything-else')).toContain('bg-nord-3');
  });

  it('maps intent phases', () => {
    expect(getIntentPhaseColor('ready')).toContain('bg-nord-14');
    expect(getIntentPhaseColor('reconciling')).toContain('bg-nord-10');
    expect(getIntentPhaseColor('pending')).toContain('bg-nord-13');
    expect(getIntentPhaseColor('degraded')).toContain('bg-nord-12');
    expect(getIntentPhaseColor('failed')).toContain('bg-nord-11');
    expect(getIntentPhaseColor('deleting')).toContain('bg-nord-3');
  });
});

describe('getIntentOwnership', () => {
  it('reads markers from config (newer payload position)', () => {
    const inst = { config: { managed_by: 'intent', intent_id: 'intent-1' } };
    expect(getIntentOwnership(inst)).toEqual({ managed: true, intentId: 'intent-1' });
  });

  it('reads markers from the top level (older payload position)', () => {
    const inst = { managed_by: 'intent', intent_id: 'intent-2' };
    expect(getIntentOwnership(inst)).toEqual({ managed: true, intentId: 'intent-2' });
  });

  it('returns unmanaged when no markers exist', () => {
    expect(getIntentOwnership({ config: {} })).toEqual({ managed: false, intentId: null });
    expect(getIntentOwnership(undefined)).toEqual({ managed: false, intentId: null });
  });
});
