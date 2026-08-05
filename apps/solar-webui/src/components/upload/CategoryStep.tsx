import { Boxes, BrainCircuit, ChevronRight } from 'lucide-react';
import { UploadCategory } from '@/api/types';
import { cn } from '@/lib/utils';

const DATASET_FORMATS = ['parquet', 'hdf5', 'json'];

interface CategoryStepProps {
  category: UploadCategory | null;
  format: string;
  onCategoryChange: (category: UploadCategory) => void;
  onFormatChange: (format: string) => void;
  onNext: () => void;
}

const REQUIREMENTS: Record<UploadCategory, string[]> = {
  model: [
    'HuggingFace-format directory: config.json + weights, or',
    'one or more .gguf files (llama.cpp quantisations)',
  ],
  dataset: ['Data files matching the declared format'],
};

export function CategoryStep({ category, format, onCategoryChange, onFormatChange, onNext }: CategoryStepProps) {
  const canContinue = category !== null && (category === 'model' || format !== '');

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <button
          type="button"
          onClick={() => onCategoryChange('model')}
          className={cn(
            'rounded-xl border p-6 text-left transition-colors',
            category === 'model' ? 'border-nord-10 bg-nord-2' : 'border-nord-3 bg-nord-1 hover:border-nord-10/50',
          )}
        >
          <BrainCircuit className="text-nord-10 mb-3" size={28} />
          <h3 className="text-lg font-semibold text-nord-6">Model</h3>
          <p className="text-sm text-nord-4 mt-1">
            A finished model directory (HF-format or GGUF) to deploy for inference.
          </p>
        </button>
        <button
          type="button"
          onClick={() => onCategoryChange('dataset')}
          className={cn(
            'rounded-xl border p-6 text-left transition-colors',
            category === 'dataset' ? 'border-nord-10 bg-nord-2' : 'border-nord-3 bg-nord-1 hover:border-nord-10/50',
          )}
        >
          <Boxes className="text-nord-15 mb-3" size={28} />
          <h3 className="text-lg font-semibold text-nord-6">Dataset</h3>
          <p className="text-sm text-nord-4 mt-1">
            Data files for training pipelines, registered in the Data Repository.
          </p>
        </button>
      </div>

      {category && (
        <div className="rounded-xl border border-nord-3 bg-nord-1 p-5">
          <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">
            {category === 'model' ? 'Model requirements' : 'Dataset requirements'}
          </h4>
          <ul className="text-sm text-nord-4 mt-2 space-y-1 list-disc list-inside">
            {REQUIREMENTS[category].map((requirement) => (
              <li key={requirement}>{requirement}</li>
            ))}
          </ul>

          {category === 'dataset' && (
            <div className="mt-4">
              <label htmlFor="dataset-format" className="block text-sm font-medium text-nord-6 mb-1">
                Format <span className="text-nord-11">*</span>
              </label>
              <select
                id="dataset-format"
                value={format}
                onChange={(event) => onFormatChange(event.target.value)}
                className="rounded-lg border border-nord-3 bg-nord-0 px-3 py-2 text-sm text-nord-6 focus:outline-none focus:ring-2 focus:ring-nord-10"
              >
                <option value="" disabled>
                  Select a format…
                </option>
                {DATASET_FORMATS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
              <p className="text-xs text-nord-4 mt-1">The Data Repository only accepts parquet, hdf5, or json.</p>
            </div>
          )}
        </div>
      )}

      <div className="flex justify-end">
        <button
          type="button"
          disabled={!canContinue}
          onClick={onNext}
          className="flex items-center gap-1 rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}
