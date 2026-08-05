import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { IntentPhaseBadge } from '@/components/IntentBadges';

describe('IntentPhaseBadge', () => {
  it('renders the phase label', () => {
    render(<IntentPhaseBadge phase="ready" />);
    expect(screen.getByText('ready')).toBeInTheDocument();
  });

  it('labels pending as queued and reconciling as updating', () => {
    const { rerender } = render(<IntentPhaseBadge phase="pending" />);
    expect(screen.getByText('queued')).toBeInTheDocument();
    rerender(<IntentPhaseBadge phase="reconciling" />);
    expect(screen.getByText('updating')).toBeInTheDocument();
  });

  it('exposes the phase tooltip', () => {
    render(<IntentPhaseBadge phase="degraded" />);
    expect(screen.getByTitle(/fewer instances than requested/i)).toBeInTheDocument();
  });

  it('renders a plain updating hint when the reconcile state is active', () => {
    render(<IntentPhaseBadge phase="reconciling" reconcile="in_progress" />);
    expect(screen.getByText('· updating')).toBeInTheDocument();
    expect(screen.queryByText(/in_progress/)).not.toBeInTheDocument();
  });

  it('omits the hint when idle or absent', () => {
    const { rerender } = render(<IntentPhaseBadge phase="ready" />);
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
    rerender(<IntentPhaseBadge phase="ready" reconcile="idle" />);
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });
});
