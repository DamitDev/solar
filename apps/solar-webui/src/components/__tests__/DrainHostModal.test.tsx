import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DrainHostModal } from '@/components/DrainHostModal';
import solarClient from '@/api/client';
import { DrainBlocker, HostDrainStatus } from '@/api/types';

const status = (blockers: DrainBlocker[] = []): HostDrainStatus => ({
  host_id: 'host-1',
  host_name: 'node-a',
  drain_state: null,
  drain_requested_at: null,
  stalled: false,
  managed_remaining: 1,
  manual_running: blockers.filter((b) => b.kind === 'manual_instance').length,
  replicas: [],
  blockers,
});

const manualBlocker: DrainBlocker = {
  kind: 'manual_instance',
  id: 'i-2',
  name: 'scratch:v1',
  detail: 'Manually created instance is running.',
};

describe('DrainHostModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('enables draining when nothing blocks it', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(status());
    const drainHost = vi.spyOn(solarClient, 'drainHost').mockResolvedValue(status());
    const onDraining = vi.fn();

    render(<DrainHostModal hostId="host-1" hostName="node-a" onClose={vi.fn()} onDraining={onDraining} />);

    expect(await screen.findByText('Nothing blocks the drain.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Drain' }));

    expect(drainHost).toHaveBeenCalledWith('host-1');
    await waitFor(() => expect(onDraining).toHaveBeenCalled());
  });

  it('lists blockers and refuses to drain while they exist', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(status([manualBlocker]));
    const drainHost = vi.spyOn(solarClient, 'drainHost');

    render(<DrainHostModal hostId="host-1" hostName="node-a" onClose={vi.fn()} onDraining={vi.fn()} />);

    expect(await screen.findByText('scratch:v1')).toBeInTheDocument();
    expect(screen.getByText(/Manual instance/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Drain' })).toBeDisabled();
    expect(drainHost).not.toHaveBeenCalled();
  });

  it('surfaces blockers that appear between opening and confirming', async () => {
    vi.spyOn(solarClient, 'getDrainStatus').mockResolvedValue(status());
    vi.spyOn(solarClient, 'drainHost').mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { detail: 'Host cannot be drained yet', blockers: [manualBlocker] } },
      },
    });
    const onDraining = vi.fn();
    vi.spyOn(console, 'error').mockImplementation(() => {});

    render(<DrainHostModal hostId="host-1" hostName="node-a" onClose={vi.fn()} onDraining={onDraining} />);

    await userEvent.click(await screen.findByRole('button', { name: 'Drain' }));

    expect(await screen.findByText('Host cannot be drained yet')).toBeInTheDocument();
    expect(screen.getByText('scratch:v1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Drain' })).toBeDisabled();
    expect(onDraining).not.toHaveBeenCalled();
  });

  it('re-checks blockers on demand', async () => {
    const getDrainStatus = vi
      .spyOn(solarClient, 'getDrainStatus')
      .mockResolvedValueOnce(status([manualBlocker]))
      .mockResolvedValueOnce(status());

    render(<DrainHostModal hostId="host-1" hostName="node-a" onClose={vi.fn()} onDraining={vi.fn()} />);

    expect(await screen.findByText('scratch:v1')).toBeInTheDocument();
    await userEvent.click(screen.getByTitle('Re-check blockers'));

    expect(await screen.findByText('Nothing blocks the drain.')).toBeInTheDocument();
    expect(getDrainStatus).toHaveBeenCalledTimes(2);
  });
});
