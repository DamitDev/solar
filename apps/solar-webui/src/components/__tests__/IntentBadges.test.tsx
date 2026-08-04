import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { IntentPhaseBadge } from '@/components/IntentBadges';

describe('IntentPhaseBadge', () => {
  it('renders the phase text', () => {
    render(<IntentPhaseBadge phase="ready" />);
    expect(screen.getByText('ready')).toBeInTheDocument();
  });

  it('exposes the phase tooltip', () => {
    render(<IntentPhaseBadge phase="degraded" />);
    expect(screen.getByTitle(/partial fulfillment/i)).toBeInTheDocument();
  });

  it('renders the reconcile hint when provided', () => {
    render(<IntentPhaseBadge phase="reconciling" reconcile="2/3 replicas" />);
    expect(screen.getByText('· 2/3 replicas')).toBeInTheDocument();
  });

  it('omits the reconcile hint when absent', () => {
    render(<IntentPhaseBadge phase="ready" />);
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });
});
