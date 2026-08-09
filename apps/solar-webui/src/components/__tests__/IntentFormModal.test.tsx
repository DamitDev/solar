import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { IntentFormModal } from '@/components/IntentFormModal';
import solarClient from '@/api/client';
import { Intent } from '@/api/types';

const intent: Intent = {
  id: 'intent-1',
  alias: 'iris:v1',
  model_source: 'huggingface://org/iris',
  replicas: 2,
  priority: 'staging',
  strategy: 'immediate',
  backend: {
    backend_type: 'llamacpp',
    model_type: 'llm',
    context_size: 4096,
    file_filters: ['*Q8_0*'],
  },
  placement: {
    roles: ['inference', 'training'],
    gpu_type: 'nvidia_cuda',
    host_allow: ['host-1'],
    host_deny: [],
  },
  resources: { vram_gb: 24, ram_gb: 64 },
  metadata: { owner: 'supernova' },
  status: {
    phase: 'ready',
    reconcile: 'idle',
    desired_replicas: 2,
    observed_replicas: 2,
    ready_replicas: 2,
    updated_replicas: 2,
    available: true,
    shortfall: 0,
    replica_set: [],
    conditions: [],
    strategy_progress: null,
    last_error: null,
  },
};

const renderEdit = (override: Partial<Intent> = {}, onSaved = vi.fn()) => {
  render(<IntentFormModal intent={{ ...intent, ...override }} onClose={vi.fn()} onSaved={onSaved} />);
  return onSaved;
};

describe('IntentFormModal in edit mode', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(solarClient, 'getHosts').mockResolvedValue([
      { id: 'host-1', name: 'node-a', gpu_type: 'nvidia_cuda', roles: ['inference', 'training'] } as any,
    ]);
  });

  it('hydrates every field from the intent', async () => {
    renderEdit();

    expect(screen.getByDisplayValue('iris:v1')).toBeInTheDocument();
    expect(screen.getByDisplayValue('huggingface://org/iris')).toBeInTheDocument();
    expect(screen.getByDisplayValue('*Q8_0*')).toBeInTheDocument();
    // Placement and resources live in collapsed sections that must open when seeded
    expect(screen.getByDisplayValue('24')).toBeInTheDocument();
    expect(screen.getByDisplayValue('64')).toBeInTheDocument();
    expect(screen.getByDisplayValue('owner')).toBeInTheDocument();
    expect(screen.getByDisplayValue('supernova')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByDisplayValue('nvidia_cuda')).toBeInTheDocument());
  });

  it('locks the alias because it is the deployment identity', () => {
    renderEdit();

    expect(screen.getByDisplayValue('iris:v1')).toBeDisabled();
    expect(screen.getByText(/cannot be changed/i)).toBeInTheDocument();
  });

  it('locks the model source and download filters — the model identity', () => {
    renderEdit();

    expect(screen.getByDisplayValue('huggingface://org/iris')).toBeDisabled();
    expect(screen.getByDisplayValue('*Q8_0*')).toBeDisabled();
    expect(screen.getByText(/model identity is fixed/i)).toBeInTheDocument();
    expect(screen.getByText(/part of the model identity/i)).toBeInTheDocument();
    // No way to add or remove filter rows in edit mode.
    expect(screen.queryByRole('button', { name: 'Add filter' })).not.toBeInTheDocument();
    expect(screen.queryByTitle('Remove filter')).not.toBeInTheDocument();
  });

  it('still submits the locked model identity unchanged', async () => {
    const updateIntent = vi.spyOn(solarClient, 'updateIntent').mockResolvedValue(intent);
    const onSaved = renderEdit();

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(updateIntent).toHaveBeenCalled());
    const [id, payload] = updateIntent.mock.calls[0];
    expect(id).toBe('intent-1');
    expect(payload.model_source).toBe('huggingface://org/iris');
    expect(payload.backend.file_filters).toEqual(['*Q8_0*']);
    expect(onSaved).toHaveBeenCalled();
  });

  it('explains what the selected strategy will do to the replicas', async () => {
    renderEdit();

    expect(screen.getByText(/all replicas stop before the replacements start/i)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByDisplayValue(/^immediate/), 'rolling');

    expect(screen.getByText(/one replica at a time/i)).toBeInTheDocument();
  });

  it('warns that saving restarts an update that is in flight', () => {
    renderEdit({
      status: {
        ...intent.status,
        strategy_progress: { strategy: 'rolling', phase: 'stopping', updated: 0, in_progress: 1, failed: 0 },
      },
    });

    expect(screen.getByText(/update is in progress/i)).toBeInTheDocument();
  });

  it('omits the update warning when nothing is updating', () => {
    renderEdit();

    expect(screen.queryByText(/update is in progress/i)).not.toBeInTheDocument();
  });

  it('PUTs the complete spec and reports the saved intent', async () => {
    const updateIntent = vi.spyOn(solarClient, 'updateIntent').mockResolvedValue({ ...intent, replicas: 3 });
    const createIntent = vi.spyOn(solarClient, 'createIntent');
    const onSaved = renderEdit();

    const replicasInput = screen.getByDisplayValue('2');
    await userEvent.clear(replicasInput);
    await userEvent.type(replicasInput, '3');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(updateIntent).toHaveBeenCalled());
    const [id, payload] = updateIntent.mock.calls[0];
    expect(id).toBe('intent-1');
    expect(payload).toMatchObject({
      alias: 'iris:v1',
      model_source: 'huggingface://org/iris',
      replicas: 3,
      priority: 'staging',
      strategy: 'immediate',
      placement: { roles: ['inference', 'training'], gpu_type: 'nvidia_cuda', host_allow: ['host-1'] },
      resources: { vram_gb: 24, ram_gb: 64 },
      metadata: { owner: 'supernova' },
    });
    expect(payload.backend).toMatchObject({ backend_type: 'llamacpp', file_filters: ['*Q8_0*'] });
    expect(createIntent).not.toHaveBeenCalled();
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ replicas: 3 }));
  });

  it('submits a dspark drafter with its block size', async () => {
    const updateIntent = vi.spyOn(solarClient, 'updateIntent').mockResolvedValue(intent);
    renderEdit();

    await userEvent.selectOptions(screen.getByLabelText('Speculative decoding'), 'draft-dspark');
    await userEvent.type(screen.getByLabelText(/Draft model/), '*DSpark*.gguf');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(updateIntent).toHaveBeenCalled());
    expect(updateIntent.mock.calls[0][1].backend).toMatchObject({
      spec_type: 'draft-dspark',
      spec_draft_model: '*DSpark*.gguf',
      spec_draft_n_max: 7,
    });
  });

  it('refuses to submit dspark without a draft model', async () => {
    const updateIntent = vi.spyOn(solarClient, 'updateIntent').mockResolvedValue(intent);
    renderEdit();

    await userEvent.selectOptions(screen.getByLabelText('Speculative decoding'), 'draft-dspark');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(updateIntent).not.toHaveBeenCalled();
  });

  it('shows server validation errors instead of closing', async () => {
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: { detail: { detail: 'Invalid intent', errors: [{ field: 'replicas', message: 'too many' }] } },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const onSaved = renderEdit();

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText('Invalid intent')).toBeInTheDocument();
    expect(screen.getByText(/too many/)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });
});

describe('IntentFormModal in create mode', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(solarClient, 'getHosts').mockResolvedValue([]);
  });

  it('leaves the alias editable and POSTs a new intent', async () => {
    const createIntent = vi.spyOn(solarClient, 'createIntent').mockResolvedValue(intent);
    const onSaved = vi.fn();

    render(
      <IntentFormModal
        initial={{ alias: 'lily:v1', model_source: 'repo://lily:v1' }}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    expect(screen.getByText('New Intent')).toBeInTheDocument();
    expect(screen.getByDisplayValue('lily:v1')).toBeEnabled();

    await userEvent.click(screen.getByRole('button', { name: 'Submit Intent' }));

    await waitFor(() => expect(createIntent).toHaveBeenCalled());
    expect(createIntent.mock.calls[0][0]).toMatchObject({ alias: 'lily:v1' });
    expect(onSaved).toHaveBeenCalled();
  });
});
