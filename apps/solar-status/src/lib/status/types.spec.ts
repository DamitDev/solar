import { describe, expect, it } from 'vitest';
import { formatInstant, formatWindow, type StatusMessage } from './types';

// Explicit zone and locale keep these tests independent of the runner: a UTC
// runner would otherwise pass the old UTC-only implementation too.
const BUDAPEST = 'Europe/Budapest';
// en-GB is CLDR's English locale whose short zone labels are abbreviations
// (CET/CEST), not numeric offsets.
const EN = 'en-GB';

describe('formatInstant', () => {
  it('formats the instant as YYYY-MM-DD HH:mm with a short zone label', () => {
    expect(formatInstant('2026-10-30T16:00:00+00:00', BUDAPEST, EN)).toBe('2026-10-30 17:00 CET');
  });

  it('follows daylight saving time', () => {
    expect(formatInstant('2026-07-01T10:00:00+00:00', BUDAPEST, EN)).toBe('2026-07-01 12:00 CEST');
  });

  it('rolls the date over when the zone crosses midnight', () => {
    expect(formatInstant('2026-01-04T23:30:00+00:00', BUDAPEST, EN)).toBe('2026-01-05 00:30 CET');
  });

  it('zero-pads every field', () => {
    expect(formatInstant('2026-01-04T04:05:00+00:00', BUDAPEST, EN)).toBe('2026-01-04 05:05 CET');
  });

  it('keeps the fixed layout and 24-hour clock for a locale with another layout', () => {
    expect(formatInstant('2026-10-30T16:00:00+00:00', BUDAPEST, 'en-US')).toMatch(
      /^2026-10-30 17:00 \S+$/
    );
  });
});

describe('formatWindow', () => {
  const message: StatusMessage = {
    id: 'a',
    body: 'x',
    type: 'maintenance',
    start_time: '2026-01-04T23:00:00+00:00',
    end_time: '2026-01-05T02:00:00+00:00'
  };

  it('formats both ends in the given zone and locale', () => {
    expect(formatWindow(message, BUDAPEST, EN)).toBe('2026-01-05 00:00 CET – 2026-01-05 03:00 CET');
  });

  it('prefixes open-ended windows with From', () => {
    expect(formatWindow({ ...message, end_time: null }, BUDAPEST, EN)).toBe(
      'From 2026-01-05 00:00 CET'
    );
  });
});
