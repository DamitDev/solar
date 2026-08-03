import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatDate(date: string | undefined): string {
  if (!date) return 'Never';
  const d = new Date(date);
  return d.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function formatDateTime(date: string | undefined): string {
  if (!date) return 'Never';
  const d = new Date(date);
  return d.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  });
}

export function formatRelativeTime(date: string | undefined): string {
  if (!date) return 'Never';
  const d = new Date(date);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffSeconds = Math.floor(diffMs / 1000);
  const diffMinutes = Math.floor(diffSeconds / 60);
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSeconds < 60) return 'just now';
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;

  return formatDate(date);
}

export function formatUptime(startedAt: string | undefined): string {
  if (!startedAt) return 'Not running';
  const start = new Date(startedAt).getTime();
  const now = Date.now();
  const diff = now - start;

  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (days > 0) return `${days}d ${hours % 24}h`;
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

export function getStatusColor(status: string): string {
  switch (status) {
    case 'running':
    case 'online':
      return 'text-nord-6 bg-nord-14'; // green
    case 'stopped':
    case 'offline':
      return 'text-nord-4 bg-nord-3'; // blue-gray
    case 'starting':
    case 'stopping':
      return 'text-nord-0 bg-nord-13'; // yellow
    case 'failed':
    case 'error':
      return 'text-nord-6 bg-nord-11'; // red
    default:
      return 'text-nord-4 bg-nord-3'; // blue-gray
  }
}

export function getMemoryColor(percent: number): string {
  if (percent < 70) {
    return 'bg-nord-14'; // green
  } else if (percent < 90) {
    return 'bg-nord-13'; // yellow
  } else {
    return 'bg-nord-11'; // red
  }
}

export function formatMemoryUsage(used: number, total: number, percent: number): string {
  return `${used.toFixed(1)} / ${total.toFixed(1)} GB (${percent.toFixed(1)}%)`;
}

export function formatDiskUsage(used: number, total: number): string {
  const percent = total > 0 ? (used / total) * 100 : 0;
  return `${used.toFixed(1)} / ${total.toFixed(1)} GB (${percent.toFixed(1)}%)`;
}

export function formatTokenCount(count: number | undefined | null): string {
  if (count === undefined || count === null || isNaN(count)) {
    return '—';
  }
  if (count === 0) {
    return '0';
  }

  const absCount = Math.abs(count);

  // Billions
  if (absCount >= 1_000_000_000) {
    const billions = absCount / 1_000_000_000;
    return `${count < 0 ? '-' : ''}${billions.toFixed(2)}B`;
  }

  // Millions
  if (absCount >= 1_000_000) {
    const millions = absCount / 1_000_000;
    return `${count < 0 ? '-' : ''}${millions.toFixed(2)}M`;
  }

  // Thousands
  if (absCount >= 1_000) {
    const thousands = absCount / 1_000;
    return `${count < 0 ? '-' : ''}${thousands.toFixed(1)}K`;
  }

  // Less than 1000, round to 1 decimal place for readability (especially for averages)
  // Remove trailing zeros for whole numbers
  const rounded = count.toFixed(1);
  return rounded.replace(/\.0$/, '');
}

export function formatBytes(bytes: number | undefined | null): string {
  if (bytes === undefined || bytes === null || isNaN(bytes)) return '—';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export function getCatalogStatusColor(status: string): string {
  switch (status) {
    case 'available':
      return 'text-nord-6 bg-nord-14'; // green — running
    case 'deployed':
      return 'text-nord-6 bg-nord-10'; // blue — on hosts, not running
    case 'unknown':
      return 'text-nord-0 bg-nord-13'; // yellow — availability unknown
    default:
      return 'text-nord-4 bg-nord-3'; // unavailable / anything else — gray
  }
}
