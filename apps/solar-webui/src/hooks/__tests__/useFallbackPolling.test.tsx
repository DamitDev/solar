/**
 * C5: the fallback poll runs only while the event stream is down.
 *
 * The whole point of the WS-first read model is that a healthy socket costs no
 * HTTP; a poll that keeps ticking while connected would undo that, and one
 * that stops when the socket drops would freeze the view.
 */

import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useFallbackPolling } from '@/hooks/useFallbackPolling';

function Probe({ enabled, intervalMs, onTick }: { enabled: boolean; intervalMs?: number; onTick: () => void }) {
  useFallbackPolling(onTick, { enabled, intervalMs });
  return null;
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
}

describe('useFallbackPolling', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not poll while the socket is connected', () => {
    const onTick = vi.fn();
    render(<Probe enabled={false} intervalMs={1000} onTick={onTick} />);

    act(() => void vi.advanceTimersByTime(5000));

    expect(onTick).not.toHaveBeenCalled();
  });

  it('polls on the given interval while disconnected', () => {
    const onTick = vi.fn();
    render(<Probe enabled intervalMs={1000} onTick={onTick} />);

    act(() => void vi.advanceTimersByTime(3000));

    expect(onTick).toHaveBeenCalledTimes(3);
  });

  it('skips ticks while the tab is hidden', () => {
    const onTick = vi.fn();
    render(<Probe enabled intervalMs={1000} onTick={onTick} />);

    setHidden(true);
    act(() => void vi.advanceTimersByTime(3000));
    expect(onTick).not.toHaveBeenCalled();

    setHidden(false);
    act(() => void vi.advanceTimersByTime(1000));
    expect(onTick).toHaveBeenCalledTimes(1);
  });

  it('stops polling when the socket reconnects', () => {
    const onTick = vi.fn();
    const { rerender } = render(<Probe enabled intervalMs={1000} onTick={onTick} />);

    act(() => void vi.advanceTimersByTime(2000));
    expect(onTick).toHaveBeenCalledTimes(2);

    rerender(<Probe enabled={false} intervalMs={1000} onTick={onTick} />);
    act(() => void vi.advanceTimersByTime(5000));

    expect(onTick).toHaveBeenCalledTimes(2);
  });

  it('clears its interval on unmount', () => {
    const onTick = vi.fn();
    const { unmount } = render(<Probe enabled intervalMs={1000} onTick={onTick} />);

    unmount();
    act(() => void vi.advanceTimersByTime(5000));

    expect(onTick).not.toHaveBeenCalled();
  });

  it('calls the latest callback without restarting the interval', () => {
    // The callback is held in a ref, so a new closure each render must not
    // reset the timer — otherwise a parent that re-renders faster than the
    // interval would never poll at all.
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Probe enabled intervalMs={1000} onTick={first} />);

    act(() => void vi.advanceTimersByTime(900));
    rerender(<Probe enabled intervalMs={1000} onTick={second} />);
    act(() => void vi.advanceTimersByTime(100));

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
