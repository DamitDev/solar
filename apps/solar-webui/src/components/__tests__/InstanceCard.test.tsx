/**
 * The instance card's model line must show the model, not the alias.
 *
 * whisper.cpp and NeMo-Speech.cpp keep the model path in `model` and have no
 * `model_id`; a `model_id || alias` fallback therefore repeated the alias on
 * the card's second line.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InstanceCard } from '@/components/InstanceCard';
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

function renderCard(config: InstanceConfig) {
  return render(
    <InstanceCard
      instance={instance('i1', config)}
      hostId="h1"
      onStart={noop}
      onStop={noop}
      onRestart={noop}
      onUpdate={noop}
      onDelete={noop}
    />,
  );
}

describe('InstanceCard model line', () => {
  it('shows the whisper.cpp model instead of repeating the alias', () => {
    renderCard({
      backend_type: 'whispercpp',
      model: '/models/ggml-tiny.bin',
      alias: 'whisper-tiny',
      host: '0.0.0.0',
      language: 'auto',
      threads: 4,
      device: 'auto',
    });

    expect(screen.getByTitle('/models/ggml-tiny.bin')).toBeInTheDocument();
    // The alias is the heading; the model line must not repeat it.
    expect(screen.getAllByText('whisper-tiny')).toHaveLength(1);
  });

  it('shows the NeMo-Speech.cpp decoder instead of repeating the alias', () => {
    renderCard({
      backend_type: 'nemospeech',
      model: '/models/decoder.gguf',
      alias: 'nemotron-stream-en',
      host: '0.0.0.0',
      language: 'en',
      diarize: false,
      device: 'auto',
    });

    expect(screen.getByTitle('/models/decoder.gguf')).toBeInTheDocument();
    expect(screen.getAllByText('nemotron-stream-en')).toHaveLength(1);
  });

  it('still shows the HuggingFace model_id', () => {
    renderCard({
      backend_type: 'huggingface_audio',
      model_id: 'openai/whisper-large-v3-hu',
      alias: 'whisper-large-v3-hu',
      host: '0.0.0.0',
      device: 'auto',
      dtype: 'auto',
      language: '',
      chunk_length_s: 30,
      batch_size: 8,
    });

    expect(screen.getByTitle('openai/whisper-large-v3-hu')).toBeInTheDocument();
  });
});
