import { render, screen } from '@testing-library/svelte';
import { expect, it } from 'vitest';
import type { ComponentStatus } from '../status/types';
import ComponentRow from './ComponentRow.svelte';

const status: ComponentStatus = { availability: 'operational', uptime_pct: 100, history: [] };
const longName = 'deepseek-v4.1-flash-1017:552b';

it('keeps the status badge on the first line next to a long name', () => {
  render(ComponentRow, {
    name: longName,
    detail: 'Alias: some-long-alias',
    windowDays: 90,
    status
  });

  const name = screen.getByRole('heading', { name: longName });
  expect(name.className).toContain('truncate');
  expect(name.className).toContain('min-w-0');
  expect(name.getAttribute('title')).toBe(longName);

  const detail = screen.getByText('Alias: some-long-alias');
  expect(detail.className).toContain('truncate');
  // The detail shrinks first so a short name like "core" is never ellipsized.
  expect(detail.className).toContain('shrink-100');
  expect(name.className).not.toContain('shrink-100');
  expect(detail.getAttribute('title')).toBe('Alias: some-long-alias');

  const badge = document.querySelector<HTMLElement>('[data-availability]')!;
  expect(badge.className).toContain('shrink-0');

  // Name column and badge share one non-wrapping flex line.
  const line = badge.parentElement!;
  expect(line.className).toContain('flex');
  expect(line.className).not.toContain('flex-wrap');
  expect(name.parentElement!.className).toContain('flex-1');
  expect(name.parentElement!.className).toContain('min-w-0');
});
