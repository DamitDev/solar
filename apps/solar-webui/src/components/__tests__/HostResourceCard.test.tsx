import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { HostResourceCard } from '@/components/HostResourceCard';
import solarClient from '@/api/client';
import { DrainState, HostDrainStatus, HostResourceSnapshot } from '@/api/types';

const snapshot = (drain_state: DrainState | null = null): HostResourceSnapshot =>
  ({
    host_id: 'host-1',
    host_name: 'node-a',
    url: 'http://node-a:8000',
    status: 'online',
    reachable: true,
    roles: ['inference'],
    gpu_type: 'nvidia_cuda',
    drain_state,
    drain_requested_at: null,
    vram_total_gb: 80,
    vram_available_gb: 40,
    instance_count: 1,
    running_instance_count: 1,
    instances: [],
    reservations: [],
    active_jobs: [],
  }) as unknown as HostResourceSnapshot;

const drainStatus = (over: Partial<HostDrainStatus> = {}): HostDrainStatus => ({
  host_id: 'host-1',
  host_name: 'node-a',
  drain_state: 'draining',
  drain_requested_at: '2026-08-04T10:00:00Z',
  stalled: false,
  managed_remaining: 1,
  manual_running: 0,
  replicas: [{ instance_id: 'i-1', alias: 'iris:v1', intent_id: 'intent-1', status: 'running', blocked_reason: null }],
  blockers: [],
  ...over,
});

describe('HostResourceCard drain controls', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('offers a drain action for a host in service', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(drainStatus());

    render(<HostResourceCard snapshot={snapshot()} />);

    expect(screen.getByRole('button', { name: 'Drain' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
    expect(solarClient.getDrainStatus).not.toHaveBeenCalled();
  });

  it('reports progress while draining', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(drainStatus());

    render(<HostResourceCard snapshot={snapshot('draining')} />);

    expect(await screen.findByText(/1 managed replica left to move/)).toBeInTheDocument();
    expect(screen.getByText('draining')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });

  it('calls out a stalled drain and why the replica cannot move', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(
      drainStatus({
        stalled: true,
        replicas: [
          {
            instance_id: 'i-1',
            alias: 'iris:v1',
            intent_id: 'intent-1',
            status: 'running',
            blocked_reason: 'No eligible host: needs vram >= 48.0 GB',
          },
        ],
      }),
    );

    render(<HostResourceCard snapshot={snapshot('draining')} />);

    expect(await screen.findByText('draining (stalled)')).toBeInTheDocument();
    expect(screen.getByText(/cannot be moved/)).toBeInTheDocument();
    expect(screen.getByText(/needs vram >= 48.0 GB/)).toBeInTheDocument();
  });

  it('reports a finished drain as safe to take offline', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(
      drainStatus({ drain_state: 'drained', managed_remaining: 0, replicas: [] }),
    );

    render(<HostResourceCard snapshot={snapshot('drained')} />);

    expect(await screen.findByText(/Safe to take offline/)).toBeInTheDocument();
    expect(screen.getByText('drained')).toBeInTheDocument();
  });

  it('resumes a host and tells the page to refetch', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(drainStatus());
    const resumeHost = vi.spyOn(solarClient, 'resumeHost').mockResolvedValue(drainStatus({ drain_state: null }));
    const onDrainChanged = vi.fn();

    render(<HostResourceCard snapshot={snapshot('draining')} onDrainChanged={onDrainChanged} />);

    await userEvent.click(screen.getByRole('button', { name: 'Resume' }));

    expect(resumeHost).toHaveBeenCalledWith('host-1');
    await waitFor(() => expect(onDrainChanged).toHaveBeenCalled());
  });
});
