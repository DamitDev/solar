import { SpecType } from '@/api/types';
import { SPEC_TYPE_DEFAULT_N_MAX, SPEC_TYPE_OPTIONS, applySpecType } from '@/lib/backendConfig';

interface SpeculativeDecodingFieldsProps {
  /** The llama.cpp config being edited. */
  value: Record<string, any>;
  onChange: (next: Record<string, any>) => void;
  /** Prefix for the input ids so two forms can coexist in the DOM. */
  idPrefix: string;
  /** An intent points at a model directory, so it selects the draft GGUF by pattern. */
  forIntent?: boolean;
}

const inputClass =
  'w-full px-3 py-2 bg-nord-1 border border-nord-3 text-nord-6 placeholder-nord-4 placeholder:opacity-60 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent';

/**
 * Speculative decoding block, shared by the add/intent form and the edit
 * modal so the two cannot offer different flags for the same backend.
 */
export function SpeculativeDecodingFields({ value, onChange, idPrefix, forIntent }: SpeculativeDecodingFieldsProps) {
  const specType = (value.spec_type as SpecType | undefined) ?? '';
  const selected = SPEC_TYPE_OPTIONS.find((option) => option.value === specType);

  const handleFieldChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, type } = e.target;
    onChange({
      ...value,
      [name]: type === 'number' ? (e.target.value === '' ? undefined : parseFloat(e.target.value)) : e.target.value,
    });
  };

  return (
    <div className="md:col-span-2 rounded-md border border-nord-3 bg-nord-2 p-3">
      <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor={`${idPrefix}-spec-type`}>
        Speculative decoding
      </label>
      <select
        id={`${idPrefix}-spec-type`}
        name="spec_type"
        value={specType}
        onChange={(e) => onChange(applySpecType(value, e.target.value as SpecType | ''))}
        className="w-full px-3 py-2 bg-nord-1 border border-nord-3 text-nord-6 rounded-md focus:ring-2 focus:ring-nord-10 focus:border-transparent"
      >
        <option value="">Disabled (default)</option>
        {SPEC_TYPE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <p className="text-xs text-nord-4 mt-1">
        {selected ? selected.description : 'Draft tokens ahead of the model to speed up generation.'}
      </p>

      {specType === 'draft-dspark' && (
        <div className="mt-3">
          <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor={`${idPrefix}-spec-draft-model`}>
            Draft model <span className="text-nord-11">*</span>
          </label>
          <input
            type="text"
            id={`${idPrefix}-spec-draft-model`}
            name="spec_draft_model"
            value={value.spec_draft_model ?? ''}
            onChange={handleFieldChange}
            placeholder={forIntent ? '*DSpark*.gguf' : '/path/to/Model-DSpark.gguf'}
            required
            className={`${inputClass} font-mono text-sm`}
          />
          <p className="text-xs text-nord-4 mt-1">
            Passed as <code>--spec-draft-model</code>. A bare filename or glob is resolved in the model directory, same
            as the model file. The drafter must be trained for this exact target model.
          </p>
        </div>
      )}

      {specType && (
        <div className="mt-3">
          <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor={`${idPrefix}-spec-draft-n-max`}>
            Maximum draft tokens
          </label>
          <input
            type="number"
            id={`${idPrefix}-spec-draft-n-max`}
            name="spec_draft_n_max"
            value={value.spec_draft_n_max ?? SPEC_TYPE_DEFAULT_N_MAX[specType]}
            onChange={handleFieldChange}
            min="1"
            step="1"
            required
            className={inputClass}
          />
          <p className="text-xs text-nord-4 mt-1">
            {specType === 'draft-dspark' ? (
              <>
                Block size passed as <code>--spec-draft-n-max</code>. llama.cpp clamps it to the size the drafter was
                trained for — 7 for the published <code>block7</code> checkpoints.
              </>
            ) : (
              <>
                Passed as <code>--spec-draft-n-max</code>. Defaults to {SPEC_TYPE_DEFAULT_N_MAX['draft-mtp']}.
              </>
            )}
          </p>
        </div>
      )}

      {specType === 'draft-dspark' && (
        <div className="mt-3">
          <label className="block text-sm font-medium text-nord-4 mb-1" htmlFor={`${idPrefix}-spec-draft-conf-min`}>
            Minimum draft confidence (Optional)
          </label>
          <input
            type="number"
            id={`${idPrefix}-spec-draft-conf-min`}
            name="spec_draft_conf_min"
            value={value.spec_draft_conf_min ?? ''}
            onChange={handleFieldChange}
            placeholder="0 = keep the whole block"
            min="0"
            max="1"
            step="0.05"
            className={inputClass}
          />
          <p className="text-xs text-nord-4 mt-1">
            Passed as <code>--spec-draft-conf-min</code>. Cuts a drafted block at the first token the drafter's
            confidence head predicts below this. Only works with a drafter that has one.
          </p>
        </div>
      )}
    </div>
  );
}
