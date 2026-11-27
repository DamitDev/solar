/**
 * The unified hosts table must render every backend's model reference.
 *
 * whisper.cpp and NeMo-Speech.cpp keep the model path in `model`, while the
 * HuggingFace backends use `model_id`; reading only one key crashed the whole
 * table through the error boundary as soon as an audio instance existed.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UnifiedTable } from '@/components/UnifiedTable';
import { getModelDisplay, truncateModel } from '@/lib/modelDisplay';
import type { Instance, InstanceConfig } from '@/api/types';

const noop = vi.fn().mockResolvedValue(undefined);

function instance(id: string, config: InstanceConfig): Instance {
  return {
    id,
    config,
    status: 'running',
    created_at: '2026-01-01T00:00:00Z',
    retry_count: 0,
  };
}

function renderTable(instances: Instance[]) {
  return render(
    <UnifiedTable
      hosts={[{ id: 'h1', name: 'solar131-local', instances }]}
      isHostReachable={() => true}
      onStartInstance={noop}
      onStopInstance={noop}
      onRestartInstance={noop}
      onUpdateInstance={noop}
      onDeleteInstance={noop}
    />,
  );
}

describe('getModelDisplay', () => {
  it('resolves the model key for every backend type', () => {
    expect(
      getModelDisplay({
        backend_type: 'llamacpp',
        model: '/models/model.gguf',
        alias: 'llm',
      } as InstanceConfig),
    ).toBe('/models/model.gguf');
    expect(
      getModelDisplay({
        backend_type: 'whispercpp',
        model: '/models/ggml-tiny.bin',
        alias: 'whisper-tiny',
      } as InstanceConfig),
    ).toBe('/models/ggml-tiny.bin');
    expect(
      getModelDisplay({
        backend_type: 'nemospeech',
        model: '/models/decoder.gguf',
        alias: 'nemotron',
      } as InstanceConfig),
    ).toBe('/models/decoder.gguf');
    expect(
      getModelDisplay({
        backend_type: 'huggingface_audio',
        model_id: 'openai/whisper-large-v3-hu',
        alias: 'whisper-hu',
      } as InstanceConfig),
    ).toBe('openai/whisper-large-v3-hu');
    expect(
      getModelDisplay({
        backend_type: 'sglang',
        model_path: '/models/sglang',
        alias: 'sglang',
      } as InstanceConfig),
    ).toBe('/models/sglang');
    expect(
      getModelDisplay({
        backend_type: 'vllm',
        model_path: '/models/vllm',
        alias: 'vllm',
      } as InstanceConfig),
    ).toBe('/models/vllm');
    // A runtime config can arrive without its model path; the alias keeps the
    // model line readable instead of blank.
    expect(
      getModelDisplay({
        backend_type: 'llamacpp',
        alias: 'llm',
      } as InstanceConfig),
    ).toBe('llm');
    // An empty model string is not a model either; it must not shadow the alias.
    expect(
      getModelDisplay({
        backend_type: 'llamacpp',
        model: '',
        alias: 'llm',
      } as InstanceConfig),
    ).toBe('llm');
  });

  it('returns an empty string for a config with no model key', () => {
    expect(getModelDisplay({ backend_type: 'futurebackend', alias: 'x' } as never)).toBe('');
  });
});

describe('truncateModel', () => {
  it('never throws on a non-string model', () => {
    expect(truncateModel(undefined)).toBe('');
    expect(truncateModel(null)).toBe('');
    expect(truncateModel(42)).toBe('');
  });

  it('still shortens long paths', () => {
    expect(truncateModel('/home/exmodify/models/whisper/ggml-tiny.bin')).toBe('.../ggml-tiny.bin');
    expect(truncateModel('short')).toBe('short');
  });
});

describe('UnifiedTable model column', () => {
  it('renders whisper.cpp, NeMo-Speech.cpp and HuggingFace audio models', () => {
    renderTable([
      instance('w1', {
        backend_type: 'whispercpp',
        model: '/home/exmodify/models/whisper/ggml-tiny.bin',
        alias: 'whisper-tiny',
        host: '0.0.0.0',
        language: 'auto',
        threads: 4,
        device: 'auto',
      }),
      instance('n1', {
        backend_type: 'nemospeech',
        model: '/home/exmodify/models/nemo-speech/decoder.gguf',
        alias: 'nemotron-stream-en',
        host: '0.0.0.0',
        language: 'en',
        diarize: false,
        device: 'auto',
      }),
      instance('h1', {
        backend_type: 'huggingface_audio',
        model_id: 'Systran/faster-whisper-large-v3-hungarian',
        alias: 'whisper-large-v3-hu',
        host: '0.0.0.0',
        device: 'auto',
        dtype: 'auto',
        language: '',
        chunk_length_s: 30,
        batch_size: 8,
      }),
    ]);

    expect(screen.getByTitle('/home/exmodify/models/whisper/ggml-tiny.bin')).toBeInTheDocument();
    expect(screen.getByTitle('/home/exmodify/models/nemo-speech/decoder.gguf')).toBeInTheDocument();
    expect(screen.getByTitle('Systran/faster-whisper-large-v3-hungarian')).toBeInTheDocument();
    expect(screen.getByText('.../ggml-tiny.bin')).toBeInTheDocument();
    expect(screen.getByText('.../decoder.gguf')).toBeInTheDocument();
    expect(screen.getByText('.../faster-whisper-large-v3-hungarian')).toBeInTheDocument();
  });

  it('renders an unknown backend with no model key instead of crashing', () => {
    renderTable([
      instance('x1', {
        backend_type: 'futurebackend',
        alias: 'future',
      } as never),
    ]);

    expect(screen.getByText('future')).toBeInTheDocument();
  });
});
