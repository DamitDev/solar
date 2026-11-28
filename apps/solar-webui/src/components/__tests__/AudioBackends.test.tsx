/**
 * The audio backends: selection, defaults and the fields each one owns.
 *
 * The two GGML speech runtimes are single-purpose, so they must not show a mode
 * dropdown; HuggingFace audio is a fourth mode on the existing backend.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AddInstanceModal } from '@/components/AddInstanceModal';
import { BackendConfigFields } from '@/components/BackendConfigFields';
import { EditInstanceModal } from '@/components/EditInstanceModal';
import { IntentFormModal } from '@/components/IntentFormModal';
import solarClient from '@/api/client';
import {
  getBackendTypeFromSelection,
  getDefaultConfig,
  stripEmptyOptionalFields,
  AUDIO_REQUEST_FIELD_SUPPORT,
} from '@/lib/backendConfig';
import {
  getBackendColor,
  getBackendLabel,
  getFullModelLabel,
  getModelCategory,
  isAudioConfig,
  isHuggingFaceAudioConfig,
  isNemoSpeechConfig,
  isWhisperCppConfig,
} from '@/api/types';

const whisperButton = () => screen.getByRole('button', { name: /whisper\.cpp/ });
const nemoButton = () => screen.getByRole('button', { name: /NeMo-Speech\.cpp/ });

describe('getBackendTypeFromSelection', () => {
  it('maps the two GGML speech runtimes to their own backend types', () => {
    expect(getBackendTypeFromSelection('whispercpp', '')).toBe('whispercpp');
    expect(getBackendTypeFromSelection('nemospeech', '')).toBe('nemospeech');
  });

  it('maps the HuggingFace audio mode', () => {
    expect(getBackendTypeFromSelection('huggingface', 'audio')).toBe('huggingface_audio');
  });

  it('leaves the existing mappings untouched', () => {
    expect(getBackendTypeFromSelection('llamacpp', 'llm')).toBe('llamacpp');
    expect(getBackendTypeFromSelection('sglang', '')).toBe('sglang');
    expect(getBackendTypeFromSelection('vllm', '')).toBe('vllm');
    expect(getBackendTypeFromSelection('huggingface', 'causal')).toBe('huggingface_causal');
    expect(getBackendTypeFromSelection('huggingface', 'classifier')).toBe('huggingface_classification');
    expect(getBackendTypeFromSelection('huggingface', 'embedding')).toBe('huggingface_embedding');
  });
});

describe('getDefaultConfig', () => {
  it('gives whisper.cpp a resolved model path and no mode field', () => {
    const config = getDefaultConfig('whispercpp', '');

    expect(config.backend_type).toBe('whispercpp');
    expect(config.model).toBe('');
    expect(config.language).toBe('auto');
    expect(config.threads).toBe(4);
    expect(config.device).toBe('auto');
    expect(config.model_type).toBeUndefined();
  });

  it('gives NeMo-Speech.cpp an encoder and diarizer slot, diarization off', () => {
    const config = getDefaultConfig('nemospeech', '');

    expect(config.backend_type).toBe('nemospeech');
    expect(config.model).toBe('');
    expect(config.encoder).toBe('');
    expect(config.diar_model).toBe('');
    expect(config.instruction).toBe('');
    expect(config.translate_to).toBe('');
    expect(config.diarize).toBe(false);
  });

  it('gives HuggingFace audio chunking instead of max_length', () => {
    const config = getDefaultConfig('huggingface', 'audio');

    expect(config.backend_type).toBe('huggingface_audio');
    expect(config.chunk_length_s).toBe(30);
    expect(config.batch_size).toBe(8);
    expect(config.max_length).toBeUndefined();
  });

  it('drops the fields an intent may not send', () => {
    const whisper = getDefaultConfig('whispercpp', '', true);
    expect(whisper.alias).toBeUndefined();
    expect(whisper.model).toBeUndefined();
    expect(whisper.model_file).toBe('');

    const nemo = getDefaultConfig('nemospeech', '', true);
    expect(nemo.alias).toBeUndefined();
    expect(nemo.model).toBeUndefined();
    expect(nemo.model_file).toBe('');

    const audio = getDefaultConfig('huggingface', 'audio', true);
    expect(audio.model_id).toBeUndefined();
    expect(audio.alias).toBeUndefined();
    expect(audio.file_filters).toEqual([]);
  });
});

describe('stripEmptyOptionalFields', () => {
  it('drops an empty language, encoder and diarizer', () => {
    const stripped = stripEmptyOptionalFields({
      backend_type: 'nemospeech',
      model: '/m.gguf',
      language: '',
      encoder: '',
      diar_model: '',
      instruction: '',
      translate_to: '',
    });

    expect(stripped.language).toBeUndefined();
    expect(stripped.encoder).toBeUndefined();
    expect(stripped.diar_model).toBeUndefined();
    expect(stripped.instruction).toBeUndefined();
    expect(stripped.translate_to).toBeUndefined();
    expect(stripped.model).toBe('/m.gguf');
  });

  it('keeps a real language', () => {
    const stripped = stripEmptyOptionalFields({ backend_type: 'whispercpp', language: 'hu' });
    expect(stripped.language).toBe('hu');
  });

  it('drops an empty file filter list', () => {
    const stripped = stripEmptyOptionalFields({ backend_type: 'huggingface_audio', file_filters: [] });
    expect(stripped.file_filters).toBeUndefined();
  });
});

describe('backend labels, colours and categories', () => {
  it('labels the three audio backends', () => {
    expect(getBackendLabel('whispercpp')).toBe('whisper.cpp');
    expect(getBackendLabel('nemospeech')).toBe('NeMo-Speech.cpp');
    expect(getBackendLabel('huggingface_audio')).toBe('HF Audio');
  });

  it('colours the three audio backends', () => {
    for (const backend of ['whispercpp', 'nemospeech', 'huggingface_audio'] as const) {
      expect(getBackendColor(backend)).toMatch(/bg-nord-/);
    }
  });

  it('puts all three under the transcription category', () => {
    for (const config of [
      { backend_type: 'whispercpp', model: '/m', alias: 'a' },
      { backend_type: 'nemospeech', model: '/m', alias: 'a' },
      { backend_type: 'huggingface_audio', model_id: '/m', alias: 'a' },
    ]) {
      expect(getModelCategory(config as never)).toBe('transcription');
      expect(getFullModelLabel(config as never)).toBe(getBackendLabel(config.backend_type as never));
    }
  });

  it('recognises the three configs and nothing else', () => {
    expect(isWhisperCppConfig({ backend_type: 'whispercpp', model: '/m', alias: 'a' } as never)).toBe(true);
    expect(isNemoSpeechConfig({ backend_type: 'nemospeech', model: '/m', alias: 'a' } as never)).toBe(true);
    expect(isHuggingFaceAudioConfig({ backend_type: 'huggingface_audio', model_id: '/m', alias: 'a' } as never)).toBe(
      true,
    );
    expect(isAudioConfig({ backend_type: 'llamacpp', alias: 'a' } as never)).toBe(false);
  });
});

describe('BackendConfigFields audio surfaces', () => {
  it('offers the two GGML speech runtimes as backends', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(whisperButton()).toBeEnabled();
    expect(nemoButton()).toBeEnabled();
  });

  it('shows no mode cards for whisper.cpp', () => {
    render(<BackendConfigFields value={{ backend_type: 'whispercpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.queryByText('Mode')).not.toBeInTheDocument();
  });

  it('shows no mode cards for NeMo-Speech.cpp', () => {
    render(<BackendConfigFields value={{ backend_type: 'nemospeech' }} onChange={vi.fn()} forIntent />);

    expect(screen.queryByText('Mode')).not.toBeInTheDocument();
  });

  it('switches to whisper.cpp defaults when the card is clicked', async () => {
    const onChange = vi.fn();
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={onChange} forIntent />);

    await userEvent.click(whisperButton());

    const next = onChange.mock.calls[0][0];
    expect(next.backend_type).toBe('whispercpp');
    expect(next.language).toBe('auto');
    expect(next.threads).toBe(4);
  });

  it('renders the whisper.cpp fields instead of the llama.cpp ones', () => {
    render(<BackendConfigFields value={{ backend_type: 'whispercpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByPlaceholderText('ggml-tiny.bin')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('/path/to/model.gguf')).not.toBeInTheDocument();
  });

  it('renders the NeMo decoder, encoder and diarizer fields', () => {
    render(<BackendConfigFields value={{ backend_type: 'nemospeech' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByPlaceholderText('*.q8_0.gguf')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('encoder.gguf')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('sortformer.gguf')).toBeInTheDocument();
    expect(screen.getByText('Instruction (Optional)')).toBeInTheDocument();
    expect(screen.getByText('Translate To (Optional)')).toBeInTheDocument();
    expect(screen.getByLabelText(/Speaker diarization/)).toBeInTheDocument();
  });

  it('offers Audio Transcription as a HuggingFace mode', () => {
    render(<BackendConfigFields value={{ backend_type: 'huggingface_causal' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByRole('button', { name: /Audio Transcription/ })).toBeInTheDocument();
  });

  it('renders the audio fields and hides max length for the audio mode', () => {
    render(<BackendConfigFields value={{ backend_type: 'huggingface_audio' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByText('Chunk Length (s)')).toBeInTheDocument();
    expect(screen.getByText('Batch Size')).toBeInTheDocument();
    expect(screen.queryByText('Max Length')).not.toBeInTheDocument();
    // file_filters is owned by IntentFormModal's Download filters, so a
    // duplicate input here would be silently overwritten on submit.
    expect(screen.queryByText('File filters (Optional)')).not.toBeInTheDocument();
  });

  it('shows an Alias input on the whisper.cpp instance form', () => {
    render(
      <BackendConfigFields
        value={{ backend_type: 'whispercpp', model: '/models/ggml-tiny.bin' }}
        onChange={vi.fn()}
        showAlias
        showModelFields
      />,
    );

    expect(screen.getByPlaceholderText('whisper-tiny:3500')).toBeInTheDocument();
  });

  it('shows an Alias input on the NeMo-Speech.cpp instance form', () => {
    render(
      <BackendConfigFields
        value={{ backend_type: 'nemospeech', model: '/models/decoder.gguf' }}
        onChange={vi.fn()}
        showAlias
        showModelFields
      />,
    );

    expect(screen.getByPlaceholderText('nemotron-stream-en:3502')).toBeInTheDocument();
  });

  it('creates a whisper.cpp instance with the alias typed into the form', async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    render(<AddInstanceModal hostId="h1" hostName="host" onClose={vi.fn()} onCreate={onCreate} />);

    await userEvent.click(whisperButton());
    await userEvent.type(screen.getByPlaceholderText('/path/to/ggml-tiny.bin'), '/models/ggml-tiny.bin');
    await userEvent.type(screen.getByPlaceholderText('whisper-tiny:3500'), 'whisper-tiny:3500');
    await userEvent.click(screen.getByRole('button', { name: /Create whisper\.cpp Instance/ }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(alertSpy).not.toHaveBeenCalled();
    expect(onCreate).toHaveBeenCalledWith(
      'h1',
      expect.objectContaining({
        backend_type: 'whispercpp',
        model: '/models/ggml-tiny.bin',
        alias: 'whisper-tiny:3500',
      }),
    );
    alertSpy.mockRestore();
  });

  it('creates a NeMo-Speech.cpp instance with the alias typed into the form', async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    render(<AddInstanceModal hostId="h1" hostName="host" onClose={vi.fn()} onCreate={onCreate} />);

    await userEvent.click(nemoButton());
    await userEvent.type(screen.getByPlaceholderText('/path/to/decoder.gguf'), '/models/decoder.gguf');
    await userEvent.type(screen.getByPlaceholderText('nemotron-stream-en:3502'), 'nemotron-stream-en:3502');
    await userEvent.click(screen.getByRole('button', { name: /Create NeMo-Speech\.cpp Instance/ }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(alertSpy).not.toHaveBeenCalled();
    expect(onCreate).toHaveBeenCalledWith(
      'h1',
      expect.objectContaining({
        backend_type: 'nemospeech',
        model: '/models/decoder.gguf',
        alias: 'nemotron-stream-en:3502',
      }),
    );
    alertSpy.mockRestore();
  });

  it('hides the intent-only file filter row on an instance form', () => {
    render(
      <BackendConfigFields
        value={{ backend_type: 'huggingface_audio' }}
        onChange={vi.fn()}
        showAlias
        showModelFields
      />,
    );

    expect(screen.queryByText('File filters (Optional)')).not.toBeInTheDocument();
    expect(screen.getByText('Chunk Length (s)')).toBeInTheDocument();
  });

  it('keeps max length for the non-audio HuggingFace modes', () => {
    render(<BackendConfigFields value={{ backend_type: 'huggingface_causal' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByText('Max Length')).toBeInTheDocument();
    expect(screen.queryByText('Chunk Length (s)')).not.toBeInTheDocument();
  });
});

describe('audio request field support note', () => {
  const note = () => screen.getByTestId('audio-request-field-support');
  const summary = () => screen.getByText('Supported Fields');
  const body = () => screen.getByTestId('audio-request-field-body');
  const items = (group: 'honored' | 'not-honored') =>
    Array.from(screen.getByTestId(`audio-request-field-${group}`).querySelectorAll('li')).map((li) => li.textContent);
  const expand = () => userEvent.click(summary());

  it('collapses the field lists behind a Supported Fields summary', () => {
    render(<BackendConfigFields value={{ backend_type: 'whispercpp' }} onChange={vi.fn()} forIntent />);

    expect(summary()).toBeInTheDocument();
    expect(note()).not.toHaveAttribute('open');
    expect((note() as HTMLDetailsElement).open).toBe(false);
    expect(body()).not.toBeVisible();
    expect(screen.getByText('Honored:')).toBeInTheDocument();
    expect(screen.getByText('Not honored:')).toBeInTheDocument();
    // The prose form the note used to have is gone.
    expect(note()).not.toHaveTextContent('Request fields — honoured');
  });

  it('names the fields whisper.cpp drops and the ones it honours', async () => {
    render(<BackendConfigFields value={{ backend_type: 'whispercpp' }} onChange={vi.fn()} forIntent />);
    await expand();

    expect(body()).toBeVisible();
    expect(items('honored')).toEqual([
      'keywords → prompt',
      'languages (one entry) → language',
      'prompt',
      'language',
      'timestamp_granularities[] (verbose_json; words always emitted)',
      'temperature_inc',
      'carry_initial_prompt',
      'suppress_non_speech',
    ]);
    expect(items('not-honored')).toEqual([
      'vad (true is refused on whisper.cpp; false is accepted and dropped)',
      'chunking_strategy',
      'include[]',
      'diarization',
      'automatic_punctuation',
      'verbatim',
      'profanity_filter',
      'speech_contexts',
      'max_speaker_count',
    ]);
  });

  it('names the fields NeMo-Speech.cpp drops and the ones it honours', async () => {
    render(<BackendConfigFields value={{ backend_type: 'nemospeech' }} onChange={vi.fn()} forIntent />);
    await expand();

    expect(items('honored')).toEqual([
      'keywords → speech_contexts',
      'languages (one entry) → language',
      'prompt',
      'language',
      'timestamp_granularities[] (verbose_json, diarized_json)',
      'diarization',
      'automatic_punctuation',
      'verbatim',
      'profanity_filter',
      'speech_contexts',
    ]);
    expect(items('not-honored')).toEqual([
      'chunking_strategy',
      'include[]',
      'temperature_inc',
      'carry_initial_prompt',
      'suppress_non_speech',
      'vad',
      'max_speaker_count',
    ]);
  });

  it('names the fields HuggingFace audio drops and the ones it honours', async () => {
    render(<BackendConfigFields value={{ backend_type: 'huggingface_audio' }} onChange={vi.fn()} forIntent />);
    await expand();

    expect(items('honored')).toEqual([
      'keywords → prompt',
      'languages (one entry) → language',
      'prompt',
      'language',
      'timestamp_granularities[] (verbose_json; word adds a second pass)',
    ]);
    expect(items('not-honored')).toEqual([
      'chunking_strategy',
      'include[]',
      'temperature_inc',
      'carry_initial_prompt',
      'suppress_non_speech',
      'vad',
      'diarization',
      'automatic_punctuation',
      'verbatim',
      'profanity_filter',
      'speech_contexts',
      'max_speaker_count',
    ]);
  });

  it('renders on both the manual instance modal and the intent form', async () => {
    const getHosts = vi.spyOn(solarClient, 'getHosts').mockResolvedValue([]);

    const manual = render(<AddInstanceModal hostId="h1" hostName="host" onClose={vi.fn()} onCreate={vi.fn()} />);
    await userEvent.click(whisperButton());
    expect(summary()).toBeInTheDocument();
    expect(body()).not.toBeVisible();
    manual.unmount();

    render(<IntentFormModal onClose={vi.fn()} onSaved={vi.fn()} />);
    await userEvent.click(whisperButton());
    expect(summary()).toBeInTheDocument();
    expect(body()).not.toBeVisible();

    getHosts.mockRestore();
  });

  it('does not render for a non-audio backend', () => {
    render(<BackendConfigFields value={{ backend_type: 'llamacpp' }} onChange={vi.fn()} forIntent />);

    expect(screen.queryByTestId('audio-request-field-support')).not.toBeInTheDocument();
  });

  it('has an entry for every audio backend', () => {
    expect(Object.keys(AUDIO_REQUEST_FIELD_SUPPORT).sort()).toEqual(['huggingface_audio', 'nemospeech', 'whispercpp']);
  });
});

describe('AddInstanceModal submit label', () => {
  it('does not name llama.cpp before a HuggingFace mode is chosen', async () => {
    render(<AddInstanceModal hostId="h1" hostName="host" onClose={vi.fn()} onCreate={vi.fn()} />);

    await userEvent.click(screen.getByRole('button', { name: /HuggingFace/ }));

    expect(screen.queryByRole('button', { name: /Create llama\.cpp Instance/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Create Instance$/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Audio Transcription/ }));
    expect(screen.getByRole('button', { name: /Create HF Audio Instance/ })).toBeInTheDocument();
  });
});

describe('required audio labels', () => {
  it('marks the whisper.cpp Model Path required', () => {
    render(<BackendConfigFields value={{ backend_type: 'whispercpp' }} onChange={vi.fn()} showAlias showModelFields />);

    expect(screen.getByText(/Model Path/).closest('label')).toHaveTextContent('*');
  });

  it('marks the NeMo Decoder GGUF required', () => {
    render(<BackendConfigFields value={{ backend_type: 'nemospeech' }} onChange={vi.fn()} showAlias showModelFields />);

    expect(screen.getByText(/Decoder GGUF/).closest('label')).toHaveTextContent('*');
  });

  it('marks the Edit modal aliases required for both audio backends', () => {
    const whisper = render(
      <EditInstanceModal
        instance={{
          id: 'w1',
          config: {
            backend_type: 'whispercpp',
            model: '/models/ggml-tiny.bin',
            alias: 'whisper-tiny',
            host: '0.0.0.0',
            language: 'auto',
            threads: 4,
            device: 'auto',
          },
          status: 'running',
          created_at: '2026-01-01T00:00:00Z',
          retry_count: 0,
        }}
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );
    expect(screen.getByText(/Alias/).closest('label')).toHaveTextContent('*');
    whisper.unmount();

    render(
      <EditInstanceModal
        instance={{
          id: 'n1',
          config: {
            backend_type: 'nemospeech',
            model: '/models/decoder.gguf',
            alias: 'nemotron-stream-en',
            host: '0.0.0.0',
            language: 'en',
            diarize: false,
            device: 'auto',
          },
          status: 'running',
          created_at: '2026-01-01T00:00:00Z',
          retry_count: 0,
        }}
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );
    expect(screen.getByText(/Alias/).closest('label')).toHaveTextContent('*');
  });
});

describe('EditInstanceModal audio submit', () => {
  const instance = (id: string, config: Record<string, unknown>) => ({
    id,
    config,
    status: 'running',
    created_at: '2026-01-01T00:00:00Z',
    retry_count: 0,
  });

  // The GGML runtimes name the model path `model`, not the HuggingFace
  // `model_id`; the edit form used to fall through to the HuggingFace branch
  // and refuse every audio edit with "Model ID and Alias are required".
  it.each([
    [
      'whisper.cpp',
      {
        backend_type: 'whispercpp',
        model: '/models/ggml-tiny.bin',
        alias: 'whisper-tiny',
        host: '0.0.0.0',
        language: 'auto',
        threads: 4,
        device: 'auto',
      },
      {
        backend_type: 'whispercpp',
        model: '/models/ggml-tiny.bin',
        alias: 'whisper-tiny',
        host: '0.0.0.0',
        language: 'auto',
        threads: 4,
        device: 'auto',
      },
    ],
    [
      'NeMo-Speech.cpp',
      {
        backend_type: 'nemospeech',
        model: '/models/decoder.gguf',
        alias: 'nemotron-stream-en',
        host: '0.0.0.0',
        encoder: '',
        diar_model: '',
        instruction: '',
        translate_to: '',
        language: 'en',
        diarize: false,
        device: 'auto',
      },
      // The empty sidecars are optional: the shared helper strips them so the
      // host receives None instead of an empty path.
      {
        backend_type: 'nemospeech',
        model: '/models/decoder.gguf',
        alias: 'nemotron-stream-en',
        host: '0.0.0.0',
        language: 'en',
        diarize: false,
        device: 'auto',
      },
    ],
  ])('submits the %s edit form without the model-id alert', async (_name, config, expected) => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    render(
      <EditInstanceModal
        instance={instance('i1', config) as never}
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={onUpdate}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Save Changes/ }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(alertSpy).not.toHaveBeenCalled();
    expect(onUpdate).toHaveBeenCalledWith('h1', 'i1', expected);
    alertSpy.mockRestore();
  });

  it('renders the NeMo instruction and translate-to inputs', () => {
    render(
      <EditInstanceModal
        instance={
          instance('n1', {
            backend_type: 'nemospeech',
            model: '/models/decoder.gguf',
            alias: 'nemotron-stream-en',
            host: '0.0.0.0',
            encoder: '',
            diar_model: '',
            instruction: '',
            translate_to: '',
            language: 'en',
            diarize: false,
            device: 'auto',
          }) as never
        }
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );

    expect(screen.getByPlaceholderText('Transcribe verbatim.')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('hu')).toBeInTheDocument();
  });

  it('submits the NeMo instruction and translate-to values', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    render(
      <EditInstanceModal
        instance={
          instance('n1', {
            backend_type: 'nemospeech',
            model: '/models/decoder.gguf',
            alias: 'nemotron-stream-en',
            host: '0.0.0.0',
            encoder: '',
            diar_model: '',
            instruction: '',
            translate_to: '',
            language: 'en',
            diarize: false,
            device: 'auto',
          }) as never
        }
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={onUpdate}
      />,
    );

    await userEvent.type(screen.getByPlaceholderText('Transcribe verbatim.'), 'Transcribe verbatim.');
    await userEvent.type(screen.getByPlaceholderText('hu'), 'de');
    await userEvent.click(screen.getByRole('button', { name: /Save Changes/ }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(alertSpy).not.toHaveBeenCalled();
    expect(onUpdate).toHaveBeenCalledWith('h1', 'n1', {
      backend_type: 'nemospeech',
      model: '/models/decoder.gguf',
      alias: 'nemotron-stream-en',
      host: '0.0.0.0',
      language: 'en',
      diarize: false,
      device: 'auto',
      instruction: 'Transcribe verbatim.',
      translate_to: 'de',
    });
    alertSpy.mockRestore();
  });

  // HuggingFace audio carries trust_remote_code like the causal branch does;
  // without the checkbox the flag could never be changed after creation.
  it('renders and submits the HuggingFace audio Trust Remote Code checkbox', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
    render(
      <EditInstanceModal
        instance={
          instance('a1', {
            backend_type: 'huggingface_audio',
            model_id: 'openai/whisper-large-v3',
            alias: 'whisper-large',
            host: '0.0.0.0',
            device: 'auto',
            dtype: 'auto',
            language: 'hu',
            chunk_length_s: 30,
            batch_size: 8,
            trust_remote_code: false,
          }) as never
        }
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={onUpdate}
      />,
    );

    const checkbox = screen.getByLabelText(/Trust Remote Code/);
    expect(checkbox).not.toBeChecked();

    await userEvent.click(checkbox);
    await userEvent.click(screen.getByRole('button', { name: /Save Changes/ }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(alertSpy).not.toHaveBeenCalled();
    expect(onUpdate).toHaveBeenCalledWith(
      'h1',
      'a1',
      expect.objectContaining({
        backend_type: 'huggingface_audio',
        trust_remote_code: true,
      }),
    );
    alertSpy.mockRestore();
  });
});

describe('audio device options', () => {
  it('offers cuda on the NeMo-Speech device select', () => {
    render(<BackendConfigFields value={{ backend_type: 'nemospeech' }} onChange={vi.fn()} forIntent />);

    expect(screen.getByRole('option', { name: 'cuda' })).toBeInTheDocument();
  });

  it('offers cuda only for NeMo-Speech in the edit modal', () => {
    const nemo = render(
      <EditInstanceModal
        instance={{
          id: 'n1',
          config: {
            backend_type: 'nemospeech',
            model: '/models/decoder.gguf',
            alias: 'nemotron-stream-en',
            host: '0.0.0.0',
            language: 'en',
            diarize: false,
            device: 'auto',
          },
          status: 'running',
          created_at: '2026-01-01T00:00:00Z',
          retry_count: 0,
        }}
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );
    expect(screen.getByRole('option', { name: 'cuda' })).toBeInTheDocument();
    nemo.unmount();

    render(
      <EditInstanceModal
        instance={{
          id: 'w1',
          config: {
            backend_type: 'whispercpp',
            model: '/models/ggml-tiny.bin',
            alias: 'whisper-tiny',
            host: '0.0.0.0',
            language: 'auto',
            threads: 4,
            device: 'auto',
          },
          status: 'running',
          created_at: '2026-01-01T00:00:00Z',
          retry_count: 0,
        }}
        hostId="h1"
        onClose={vi.fn()}
        onUpdate={vi.fn()}
      />,
    );
    // whisper.cpp has no cuda device; its only device flag is --no-gpu.
    expect(screen.queryByRole('option', { name: 'cuda' })).not.toBeInTheDocument();
  });
});

describe('required model file on a pull-source intent', () => {
  const renderIntent = async (source: string, backend: 'whisper' | 'nemo' = 'whisper') => {
    vi.spyOn(solarClient, 'getHosts').mockResolvedValue([]);
    render(<IntentFormModal onClose={vi.fn()} onSaved={vi.fn()} />);
    await userEvent.type(screen.getByPlaceholderText('repo://model-name:v1'), source);
    await userEvent.click(backend === 'whisper' ? whisperButton() : nemoButton());
  };

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('marks the whisper.cpp Model file required for a pull source', async () => {
    await renderIntent('repo://models/whisper');

    expect(screen.getByText(/Model file/).closest('label')).toHaveTextContent('*');
  });

  it('marks the NeMo Decoder file required for a pull source', async () => {
    await renderIntent('huggingface://org/model', 'nemo');

    expect(screen.getByText(/Decoder file/).closest('label')).toHaveTextContent('*');
  });

  it('does not mark the Model file required for a local source', async () => {
    await renderIntent('local:///models/whisper');

    expect(screen.getByText(/Model file/).closest('label')).not.toHaveTextContent('*');
  });
});
