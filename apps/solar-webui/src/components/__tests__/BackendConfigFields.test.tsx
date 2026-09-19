import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { BackendConfigFields } from '@/components/BackendConfigFields';

const sglangButton = () => screen.getByRole('button', { name: /SGLang/ });
const vllmButton = () => screen.getByRole('button', { name: /vLLM/ });

describe('BackendConfigFields backend selection', () => {
  it('offers SGLang alongside llama.cpp and HuggingFace', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByRole('button', { name: /llama\.cpp/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /HuggingFace/ })).toBeInTheDocument();
    expect(sglangButton()).toBeEnabled();
  });

  it('offers vLLM alongside the other backends', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(vllmButton()).toBeEnabled();
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

  it('switches the backend object to vllm defaults when selected', async () => {
    const onChange = vi.fn();
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={onChange} forIntent />);

    await userEvent.click(vllmButton());

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(next.backend_type).toBe('vllm');
    // An intent resolves model_source into model_path server-side.
    expect(next.model_path).toBeUndefined();
    expect(next.tensor_parallel_size).toBe(1);
  });

  it('shows no mode cards for vLLM either', () => {
    render(<BackendConfigFields value={{ backend_type: 'vllm' }} onChange={vi.fn()} forIntent />);

    expect(screen.queryByText('Mode')).not.toBeInTheDocument();
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
        disabledReasons={{ sglang: 'Requires an NVIDIA host with SGLang installed' }}
      />,
    );

    const button = sglangButton();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Requires an NVIDIA host with SGLang installed');
    expect(screen.getByText('Requires an NVIDIA host with SGLang installed')).toBeInTheDocument();

    await userEvent.click(button);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('disables vLLM on a host that cannot run it, and says why', async () => {
    const onChange = vi.fn();
    render(
      <BackendConfigFields
        value={{ backend_type: 'llamacpp' }}
        onChange={onChange}
        disabledBackends={['vllm']}
        disabledReasons={{ vllm: 'vLLM is not installed on gpu-1' }}
      />,
    );

    const button = vllmButton();
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'vLLM is not installed on gpu-1');

    await userEvent.click(button);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders the SGLang fields when the stored backend is sglang', () => {
    render(<BackendConfigFields value={{ backend_type: 'sglang', tp_size: 8 }} onChange={vi.fn()} forIntent />);

    expect(screen.getByLabelText('Tensor Parallel Size')).toHaveValue(8);
    expect(screen.getByLabelText('Extra Arguments')).toBeInTheDocument();
    expect(screen.getByLabelText('Extra Environment')).toBeInTheDocument();
  });

  it('renders the vLLM fields when the stored backend is vllm', () => {
    render(
      <BackendConfigFields value={{ backend_type: 'vllm', tensor_parallel_size: 4 }} onChange={vi.fn()} forIntent />,
    );

    expect(screen.getByLabelText('Tensor Parallel Size')).toHaveValue(4);
    expect(screen.getByLabelText('Speculative Config')).toBeInTheDocument();
    expect(screen.getByLabelText('Extra Arguments')).toBeInTheDocument();
    expect(screen.getByLabelText('Extra Environment')).toBeInTheDocument();
  });

  it('accepts 262144 as context length because min is the HTML step base', () => {
    render(
      <BackendConfigFields value={{ backend_type: 'sglang', context_length: 262144 }} onChange={vi.fn()} forIntent />,
    );

    const input = screen.getByLabelText('Context Length') as HTMLInputElement;
    const min = Number(input.min);
    const step = Number(input.step);
    expect(step).toBe(1024);
    expect((262144 - min) % step).toBe(0);
    expect(input.validity.stepMismatch).toBe(false);
    expect(input.validity.rangeUnderflow).toBe(false);
  });
});
