import { render } from '@testing-library/svelte';
import { expect, it } from 'vitest';
import type { UptimePoint } from '../status/types';
import UptimeBar from './UptimeBar.svelte';

function history(length: number): UptimePoint[] {
  return Array.from({ length }, (_, index) => ({
    ts: new Date(Date.UTC(2026, 0, 1 + index)).toISOString(),
    uptime: 1,
    samples: 10
  }));
}

function tooltips(points: UptimePoint[]): HTMLElement[] {
  const { container } = render(UptimeBar, { history: points });
  return [...container.querySelectorAll<HTMLElement>('[role="tooltip"]')];
}

it('anchors the outermost tooltips to the container edges for a full history', () => {
  const tips = tooltips(history(90));

  expect(tips[0].className).toContain('left-0');
  expect(tips[45].className).toContain('left-1/2');
  expect(tips[89].className).toContain('right-0');
});

it('anchors the final bar right when the history is shorter than the edge window', () => {
  const tips = tooltips(history(4));

  expect(tips[0].className).toContain('left-0');
  // A left-anchored tooltip on the last bar would overflow the container.
  expect(tips[3].className).toContain('right-0');
  expect(tips[3].className).not.toContain('left-0');
});
