import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { MetadataStep } from '@/components/upload/MetadataStep';

const baseProps = {
  category: 'model' as const,
  format: '',
  submitting: false,
  submitError: null,
  onBack: vi.fn(),
  onSubmit: vi.fn(),
};

describe('MetadataStep', () => {
  it('validates the artifact name against the repo pattern', async () => {
    render(<MetadataStep {...baseProps} />);
    const nameInput = screen.getByLabelText(/Artifact name/);
    const submit = screen.getByRole('button', { name: /Start upload/ });

    await userEvent.type(nameInput, 'BadName!');
    expect(screen.getByText(/Lowercase letters, digits/)).toBeInTheDocument();
    expect(submit).toBeDisabled();

    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'iris-osl');
    expect(submit).toBeEnabled();
  });

  it('rejects the reserved version "latest"', async () => {
    render(<MetadataStep {...baseProps} />);
    await userEvent.type(screen.getByLabelText(/Artifact name/), 'iris-osl');
    const versionInput = screen.getByLabelText(/Version/);
    const submit = screen.getByRole('button', { name: /Start upload/ });

    await userEvent.type(versionInput, 'latest');
    expect(screen.getByText(/"latest" is reserved/)).toBeInTheDocument();
    expect(submit).toBeDisabled();

    await userEvent.clear(versionInput);
    await userEvent.type(versionInput, 'v2');
    expect(submit).toBeEnabled();
  });

  it('submits the built metadata for a model', async () => {
    const onSubmit = vi.fn();
    render(<MetadataStep {...baseProps} onSubmit={onSubmit} />);

    await userEvent.type(screen.getByLabelText(/Artifact name/), 'iris-osl');
    await userEvent.type(screen.getByLabelText(/Description/), 'A fine-tuned model');
    await userEvent.type(screen.getByLabelText(/Base model/), 'repo://base:v1');
    // fireEvent.change — userEvent.type() would parse the braces as keyboard syntax.
    fireEvent.change(screen.getByLabelText(/Training config/), {
      target: { value: '{"epochs": 3}' },
    });
    await userEvent.click(screen.getByRole('button', { name: /Start upload/ }));

    expect(onSubmit).toHaveBeenCalledWith(
      'iris-osl',
      undefined,
      expect.objectContaining({
        description: 'A fine-tuned model',
        lineage: { parent_model: 'repo://base:v1' },
        training_config: { epochs: 3 },
      }),
    );
  });
});
