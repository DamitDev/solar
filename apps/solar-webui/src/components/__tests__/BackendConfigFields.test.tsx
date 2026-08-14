import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { BackendConfigFields } from '@/components/BackendConfigFields';

const sglangButton = () => screen.getByRole('button', { name: /SGLang/ });

describe('BackendConfigFields backend selection', () => {
  it('offers SGLang alongside llama.cpp and HuggingFace', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByRole('button', { name: /llama\.cpp/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /HuggingFace/ })).toBeInTheDocument();
    expect(sglangButton()).toBeEnabled();
  });

  it('switches the backend object to sglang defaults when selected', async () => {
    const onChange = vi.fn();
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={onChange} forIntent />);

    await userEvent.click(sglangButton());

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(next.backend_type).toBe('sglang');
    // An intent resolves model_source into model_path server-side.
    expect(next.model_path).toBeUndefined();
  });

  it('shows no mode cards for SGLang, which serves generation only', () => {
    render(<BackendConfigFields value={{ backend_type: 'sglang' }} onChange={vi.fn()} forIntent />);

    expect(screen.queryByText('Mode')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Text Generation/ })).not.toBeInTheDocument();
  });

  it('keeps the mode cards for the backends that have several', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByText('Mode')).toBeInTheDocument();
  });

  it('disables SGLang on a host that cannot run it, and says why', async () => {
    const onChange = vi.fn();
    render(
      <BackendConfigFields
        value={{ backend_type: 'llamacpp' }}
        onChange={onChange}
        disabledBackends={['sglang']}
        disabledReason="Requires an NVIDIA host with SGLang installed"
      />,
    );

    const button = sglangButton();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Requires an NVIDIA host with SGLang installed');
    expect(screen.getByText('Requires an NVIDIA host with SGLang installed')).toBeInTheDocument();

    await userEvent.click(button);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders the SGLang fields when the stored backend is sglang', () => {
    render(<BackendConfigFields value={{ backend_type: 'sglang', tp_size: 8 }} onChange={vi.fn()} forIntent />);

    expect(screen.getByLabelText('Tensor Parallel Size')).toHaveValue(8);
    expect(screen.getByLabelText('Extra Arguments')).toBeInTheDocument();
    expect(screen.getByLabelText('Extra Environment')).toBeInTheDocument();
  });
});
