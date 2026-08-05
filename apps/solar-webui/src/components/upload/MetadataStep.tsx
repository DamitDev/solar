import { useState } from 'react';
import { ChevronLeft, CloudUpload } from 'lucide-react';
import { UploadCategory } from '@/api/types';
import { cn } from '@/lib/utils';
import { ARTIFACT_NAME_RE, ARTIFACT_VERSION_RE } from '@/lib/uploadPaths';

interface MetadataStepProps {
  category: UploadCategory | null;
  format: string;
  submitting: boolean;
  submitError: string | null;
  onBack: () => void;
  onSubmit: (name: string, version: string | undefined, metadata: Record<string, any>) => void;
}

function parseJsonField(raw: string): { value?: Record<string, any>; error?: string } {
  const trimmed = raw.trim();
  if (!trimmed) return {};
  try {
    const parsed = JSON.parse(trimmed);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return { error: 'Must be a JSON object' };
    }
    return { value: parsed };
  } catch {
    return { error: 'Invalid JSON' };
  }
}

export function MetadataStep({ category, format, submitting, submitError, onBack, onSubmit }: MetadataStepProps) {
  const [name, setName] = useState('');
  const [version, setVersion] = useState('');
  const [description, setDescription] = useState('');
  const [baseModel, setBaseModel] = useState('');
  const [sourceDataset, setSourceDataset] = useState('');
  const [trainingConfig, setTrainingConfig] = useState('');
  const [evalMetrics, setEvalMetrics] = useState('');
  const [recordCount, setRecordCount] = useState('');
  const [source, setSource] = useState('');

  const nameError =
    name.length > 0 && !ARTIFACT_NAME_RE.test(name)
      ? 'Lowercase letters, digits, dots, hyphens, underscores; max 255 characters.'
      : null;
  const versionError =
    version.length > 0 && (version.toLowerCase() === 'latest' || !ARTIFACT_VERSION_RE.test(version))
      ? 'Use 1-128 alphanumeric characters, dots, hyphens, underscores — "latest" is reserved.'
      : null;
  const trainingConfigResult = parseJsonField(trainingConfig);
  const evalMetricsResult = parseJsonField(evalMetrics);
  const recordCountError =
    recordCount.length > 0 && (Number.isNaN(Number(recordCount)) || Number(recordCount) < 0)
      ? 'Record count must be a non-negative number.'
      : null;

  const canSubmit =
    name.length > 0 &&
    nameError === null &&
    versionError === null &&
    trainingConfigResult.error === undefined &&
    evalMetricsResult.error === undefined &&
    recordCountError === null &&
    !submitting;

  const buildMetadata = (): Record<string, any> => {
    const metadata: Record<string, any> = {};
    if (description.trim()) metadata.description = description.trim();
    if (category === 'model') {
      const lineage: Record<string, string> = {};
      if (baseModel.trim()) lineage.parent_model = baseModel.trim();
      if (sourceDataset.trim()) lineage.source_dataset = sourceDataset.trim();
      if (Object.keys(lineage).length > 0) metadata.lineage = lineage;
      if (trainingConfigResult.value) metadata.training_config = trainingConfigResult.value;
      if (evalMetricsResult.value) metadata.eval_metrics = evalMetricsResult.value;
    } else {
      metadata.format = format;
      if (recordCount.trim()) metadata.record_count = Number(recordCount);
      if (source.trim()) metadata.source = source.trim();
    }
    return metadata;
  };

  const inputClass = (hasError: boolean) =>
    cn(
      'w-full rounded-lg border bg-nord-0 px-3 py-2 text-sm text-nord-6 focus:outline-none focus:ring-2 focus:ring-nord-10',
      hasError ? 'border-nord-11' : 'border-nord-3',
    );

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="artifact-name" className="block text-sm font-medium text-nord-6 mb-1">
            Artifact name <span className="text-nord-11">*</span>
          </label>
          <input
            id="artifact-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. iris-osl"
            className={inputClass(nameError !== null)}
          />
          {nameError && <p className="text-xs text-nord-11 mt-1">{nameError}</p>}
        </div>
        <div>
          <label htmlFor="artifact-version" className="block text-sm font-medium text-nord-6 mb-1">
            Version <span className="text-nord-4 font-normal">(optional)</span>
          </label>
          <input
            id="artifact-version"
            value={version}
            onChange={(event) => setVersion(event.target.value)}
            placeholder="e.g. v1 — omitted resolves to the next v{n}"
            className={inputClass(versionError !== null)}
          />
          {versionError && <p className="text-xs text-nord-11 mt-1">{versionError}</p>}
        </div>
      </div>

      <div className="rounded-xl border border-nord-3 bg-nord-1 p-5 space-y-4">
        <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">
          {category === 'model' ? 'Model metadata' : 'Dataset metadata'}
        </h4>

        <div>
          <label htmlFor="meta-description" className="block text-sm font-medium text-nord-6 mb-1">
            Description
          </label>
          <textarea
            id="meta-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            rows={2}
            className={inputClass(false)}
          />
        </div>

        {category === 'model' ? (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label htmlFor="meta-base-model" className="block text-sm font-medium text-nord-6 mb-1">
                  Base model <span className="text-nord-4 font-normal">(lineage)</span>
                </label>
                <input
                  id="meta-base-model"
                  value={baseModel}
                  onChange={(event) => setBaseModel(event.target.value)}
                  placeholder="repo://name:version"
                  className={inputClass(false)}
                />
              </div>
              <div>
                <label htmlFor="meta-source-dataset" className="block text-sm font-medium text-nord-6 mb-1">
                  Source dataset <span className="text-nord-4 font-normal">(lineage)</span>
                </label>
                <input
                  id="meta-source-dataset"
                  value={sourceDataset}
                  onChange={(event) => setSourceDataset(event.target.value)}
                  placeholder="repo://name:version"
                  className={inputClass(false)}
                />
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label htmlFor="meta-training-config" className="block text-sm font-medium text-nord-6 mb-1">
                  Training config <span className="text-nord-4 font-normal">(JSON, optional)</span>
                </label>
                <textarea
                  id="meta-training-config"
                  value={trainingConfig}
                  onChange={(event) => setTrainingConfig(event.target.value)}
                  rows={4}
                  placeholder='{"epochs": 3, "learning_rate": 1e-5}'
                  className={inputClass(trainingConfigResult.error !== undefined)}
                />
                {trainingConfigResult.error && (
                  <p className="text-xs text-nord-11 mt-1">{trainingConfigResult.error}</p>
                )}
              </div>
              <div>
                <label htmlFor="meta-eval-metrics" className="block text-sm font-medium text-nord-6 mb-1">
                  Eval metrics <span className="text-nord-4 font-normal">(JSON, optional)</span>
                </label>
                <textarea
                  id="meta-eval-metrics"
                  value={evalMetrics}
                  onChange={(event) => setEvalMetrics(event.target.value)}
                  rows={4}
                  placeholder='{"accuracy": 0.95}'
                  className={inputClass(evalMetricsResult.error !== undefined)}
                />
                {evalMetricsResult.error && <p className="text-xs text-nord-11 mt-1">{evalMetricsResult.error}</p>}
              </div>
            </div>
          </>
        ) : (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="block text-sm font-medium text-nord-6 mb-1">Format</label>
                <input value={format} disabled className={cn(inputClass(false), 'opacity-60')} />
              </div>
              <div>
                <label htmlFor="meta-record-count" className="block text-sm font-medium text-nord-6 mb-1">
                  Record count <span className="text-nord-4 font-normal">(optional)</span>
                </label>
                <input
                  id="meta-record-count"
                  value={recordCount}
                  onChange={(event) => setRecordCount(event.target.value)}
                  placeholder="e.g. 120000"
                  className={inputClass(recordCountError !== null)}
                />
                {recordCountError && <p className="text-xs text-nord-11 mt-1">{recordCountError}</p>}
              </div>
            </div>
            <div>
              <label htmlFor="meta-source" className="block text-sm font-medium text-nord-6 mb-1">
                Source <span className="text-nord-4 font-normal">(optional)</span>
              </label>
              <input
                id="meta-source"
                value={source}
                onChange={(event) => setSource(event.target.value)}
                placeholder="Where the data came from"
                className={inputClass(false)}
              />
            </div>
          </>
        )}
      </div>

      <div className="flex items-center justify-between">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1 rounded-lg border border-nord-3 px-4 py-2 text-sm font-medium text-nord-4 transition-colors hover:bg-nord-2"
        >
          <ChevronLeft size={16} /> Back
        </button>
        <div className="flex items-center gap-3">
          {submitError && <p className="text-sm text-nord-11">{submitError}</p>}
          <button
            type="button"
            disabled={!canSubmit}
            onClick={() => onSubmit(name, version || undefined, buildMetadata())}
            className="flex items-center gap-2 rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <CloudUpload size={16} />
            {submitting ? 'Creating session…' : 'Start upload'}
          </button>
        </div>
      </div>
    </div>
  );
}
