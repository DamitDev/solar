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
    // C3: the error renders inline next to the offending field, and exactly
    // once — the banner lists only fields with no inline slot, so a matched
    // error does not also appear there and read as two problems.
    expect(screen.getByText(/too many/)).toBeInTheDocument();
    expect(screen.queryByText('replicas')).not.toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('keeps unmatched fields in the banner, since they have no input to sit next to', async () => {
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'metadata.owner', message: 'reserved key' }],
          },
        },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderEdit();

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText('Invalid intent')).toBeInTheDocument();
    expect(screen.getByText('metadata.owner')).toBeInTheDocument();
    expect(screen.getByText(/reserved key/)).toBeInTheDocument();
  });

  it('clears the previous attempt errors when submitting again', async () => {
    const updateIntent = vi
      .spyOn(solarClient, 'updateIntent')
      .mockRejectedValueOnce({
        response: {
          status: 422,
          data: { detail: { detail: 'Invalid intent', errors: [{ field: 'replicas', message: 'too many' }] } },
        },
      })
      .mockResolvedValueOnce({ ...intent, replicas: 1 });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderEdit();

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText(/too many/)).toBeInTheDocument();

    const replicasInput = screen.getByDisplayValue('2');
    await userEvent.clear(replicasInput);
    await userEvent.type(replicasInput, '1');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(updateIntent).toHaveBeenCalledTimes(2));
    // A stale error would leave the field marked invalid after it was fixed.
    await waitFor(() => expect(screen.queryByText(/too many/)).not.toBeInTheDocument());
  });

  it('keeps a 422 visible when the field owns a slot that did not mount', async () => {
    // backend.mmproj only renders a slot in llm mode, and the server only
    // rejects mmproj outside llm mode — the two conditions are exclusive, so a
    // hand-maintained "has an inline slot" list drops the error entirely and
    // the user gets a red box reading "Invalid intent" and nothing else.
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'backend.mmproj', message: "mmproj is meaningless for model_type 'embedding'" }],
          },
        },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderEdit({ backend: { backend_type: 'llamacpp', model_type: 'embedding', mmproj: 'mmproj.gguf' } });

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText('Invalid intent')).toBeInTheDocument();
    expect(screen.getByText('backend.mmproj')).toBeInTheDocument();
    expect(screen.getByText(/meaningless for model_type/)).toBeInTheDocument();
  });

  it('keeps a model_file error visible when the backend is not llama.cpp', async () => {
    // The model_file slot lives inside the llama.cpp branch, so it does not
    // mount on a HuggingFace backend — which is the only configuration that
    // produces the error. Client-side validation gets there before the server
    // here, and used to set a field error with no slot and no banner, so the
    // submit button did nothing at all.
    const updateIntent = vi.spyOn(solarClient, 'updateIntent');
    renderEdit({ backend: { backend_type: 'huggingface_causal', model_file: 'm.gguf' } });

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText('backend.model_file')).toBeInTheDocument();
    expect(screen.getByText(/only available for the llama\.cpp backend/)).toBeInTheDocument();
    expect(updateIntent).not.toHaveBeenCalled();
  });

  it('keeps a 422 on device visible when the backend is llama.cpp', async () => {
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'backend.device', message: 'device is only supported for huggingface_* backends' }],
          },
        },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderEdit({ backend: { backend_type: 'llamacpp', model_type: 'llm', device: 'cuda' } });

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText('Invalid intent')).toBeInTheDocument();
    expect(screen.getByText('backend.device')).toBeInTheDocument();
    expect(screen.getByText(/only supported for huggingface_\* backends/)).toBeInTheDocument();
  });

  it('renders an error inline, not in the banner, when its slot did mount', async () => {
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'backend.mmproj', message: 'mmproj must be a non-empty string' }],
          },
        },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderEdit({ backend: { backend_type: 'llamacpp', model_type: 'llm', mmproj: 'mmproj.gguf' } });

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText(/must be a non-empty string/)).toBeInTheDocument();
    // Shown once, next to the input — not also listed in the banner.
    expect(screen.queryByText('backend.mmproj')).not.toBeInTheDocument();
  });

  it('opens a collapsed section that holds an error', async () => {
    vi.spyOn(solarClient, 'updateIntent').mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: {
            detail: 'Invalid intent',
            errors: [{ field: 'resources.vram_gb', message: 'vram_gb exceeds every host' }],
          },
        },
      },
    });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    // No seeded resources or metadata, so the section starts closed and the
    // inline error would render into a section the user cannot see.
    renderEdit({ resources: { vram_gb: null, ram_gb: null } as any, metadata: {} });

    const section = screen.getByText(/Resources & Metadata/).closest('details') as HTMLDetailsElement;
    expect(section.open).toBe(false);

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText(/exceeds every host/)).toBeInTheDocument();
    await waitFor(() => expect(section.open).toBe(true));
  });

  it('rejects an unknown gpu_type before the round trip', async () => {
    const updateIntent = vi.spyOn(solarClient, 'updateIntent');
    renderEdit({ placement: { roles: ['inference'], gpu_type: 'rocm', host_allow: [], host_deny: [] } as any });

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByText(/Unknown GPU type 'rocm'/)).toBeInTheDocument();
    expect(updateIntent).not.toHaveBeenCalled();
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
