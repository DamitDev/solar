import { describe, expect, it } from 'vitest';
import { formatInstant, formatWindow, type StatusMessage } from './types';

// Explicit zones and locales keep these tests independent of the runner: a UTC
// runner would otherwise pass the old UTC-only implementation too.
const BUDAPEST = 'Europe/Budapest';

// ICU may use a (narrow) no-break space before AM/PM, depending on its version.
const plain = (text: string) => text.replace(/\s/g, ' ');

describe('formatInstant', () => {
  it('converts to the given time zone without a zone label', () => {
    expect(formatInstant('2026-10-30T16:00:00+00:00', BUDAPEST, 'de-DE')).toBe('30.10.2026, 17:00');
  });

  it('follows daylight saving time', () => {
    expect(formatInstant('2026-07-01T10:00:00+00:00', BUDAPEST, 'de-DE')).toBe('01.07.2026, 12:00');
  });

  it('rolls the date over when the zone crosses midnight', () => {
    expect(formatInstant('2026-01-04T23:30:00+00:00', BUDAPEST, 'de-DE')).toBe('05.01.2026, 00:30');
  });

  it("uses the locale's native layout", () => {
    const iso = '2026-10-30T16:00:00+00:00';
    expect(plain(formatInstant(iso, BUDAPEST, 'en-US'))).toBe('10/30/2026, 05:00 PM');
    expect(formatInstant(iso, BUDAPEST, 'en-GB')).toBe('30/10/2026, 17:00');
    expect(formatInstant(iso, BUDAPEST, 'hu-HU')).toBe('2026. 10. 30. 17:00');
    expect(formatInstant(iso, BUDAPEST, 'ja-JP')).toBe('2026/10/30 17:00');
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
    expect(formatWindow(message, BUDAPEST, 'de-DE')).toBe('05.01.2026, 00:00 – 05.01.2026, 03:00');
  });

  it('prefixes open-ended windows with From', () => {
    expect(formatWindow({ ...message, end_time: null }, BUDAPEST, 'de-DE')).toBe(
      'From 05.01.2026, 00:00'
    );
  });
});
