import { render, screen } from '@testing-library/svelte';
import { expect, it } from 'vitest';
import type { StatusMessage } from '../status/types';
import MessageItem from './MessageItem.svelte';

function renderBody(body: string) {
  const message: StatusMessage = {
    id: 'm1',
    type: 'incident',
    body,
    start_time: '2026-10-30T16:00:00Z',
    end_time: null
  };
  render(MessageItem, { message });
  return screen.getByTestId('message-body');
}

it('renders bold text', () => {
  const body = renderBody('Core is **degraded**');
  expect(body.querySelector('strong')).toHaveTextContent('degraded');
});

it('renders lists', () => {
  const body = renderBody('- one\n- two');
  expect([...body.querySelectorAll('ul > li')].map((li) => li.textContent)).toEqual(['one', 'two']);
});

it('renders links that open safely in a new tab', () => {
  const body = renderBody('See [the report](https://example.com/report)');
  const link = body.querySelector('a');
  expect(link).toHaveAttribute('href', 'https://example.com/report');
  expect(link).toHaveAttribute('target', '_blank');
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
});

it('renders inline code', () => {
  const body = renderBody('Restart `solar-host` now');
  expect(body.querySelector('code')).toHaveTextContent('solar-host');
});

it('keeps single newlines as line breaks', () => {
  const body = renderBody('first line\nsecond line');
  expect(body.querySelectorAll('br')).toHaveLength(1);
});

it('strips script payloads and event handlers', () => {
  const body = renderBody(
    'safe <script>window.__pwned = true</script> text <img src=x onerror="window.__pwned = true">'
  );
  expect(body.querySelector('script')).toBeNull();
  expect(body.querySelector('[onerror]')).toBeNull();
  expect(body).toHaveTextContent('safe');
  expect(body).not.toHaveTextContent('__pwned');
});

it('strips javascript: links', () => {
  const body = renderBody('[click](javascript:alert(1))');
  expect(body.querySelector('a')?.getAttribute('href') ?? null).toBeNull();
});

it('leaves images out', () => {
  const body = renderBody('![tracker](https://example.com/pixel.png)');
  expect(body.querySelector('img')).toBeNull();
});

it('demotes headings to h3', () => {
  const body = renderBody('# Big\n\n## Medium');
  expect(body.querySelector('h1, h2')).toBeNull();
  expect(body.querySelectorAll('h3')).toHaveLength(2);
});
