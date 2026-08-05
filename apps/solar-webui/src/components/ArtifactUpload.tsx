import { useState } from 'react';
import { UploadCloud } from 'lucide-react';
import solarClient from '@/api/client';
import { CreateUploadResponse, UploadCategory } from '@/api/types';
import { cn } from '@/lib/utils';
import { SelectedUploadFile } from '@/lib/uploadPaths';
import { CategoryStep } from './upload/CategoryStep';
import { FolderStep } from './upload/FolderStep';
import { MetadataStep } from './upload/MetadataStep';
import { ProgressStep } from './upload/ProgressStep';

type WizardStep = 'category' | 'folder' | 'metadata' | 'progress';

const STEP_LABELS: Array<{ key: WizardStep; label: string }> = [
  { key: 'category', label: 'Category' },
  { key: 'folder', label: 'Folder' },
  { key: 'metadata', label: 'Metadata' },
  { key: 'progress', label: 'Upload' },
];

export function ArtifactUpload() {
  const [step, setStep] = useState<WizardStep>('category');
  const [category, setCategory] = useState<UploadCategory | null>(null);
  const [format, setFormat] = useState('');
  const [entries, setEntries] = useState<SelectedUploadFile[]>([]);
  const [session, setSession] = useState<CreateUploadResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [aborted, setAborted] = useState(false);

  const handleSubmit = async (name: string, version: string | undefined, metadata: Record<string, any>) => {
    if (!category) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await solarClient.createUpload({
        category,
        name,
        version,
        files: entries.map((entry) => ({ path: entry.path, size: entry.file.size })),
        metadata,
      });
      setSession(created);
      setStep('progress');
    } catch (error: any) {
      setSubmitError(error?.response?.data?.detail || error?.message || 'Failed to create upload session');
    } finally {
      setSubmitting(false);
    }
  };

  const stepIndex = STEP_LABELS.findIndex((entry) => entry.key === step);

  return (
    <div className="bg-nord-0">
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center gap-3">
            <UploadCloud className="text-nord-10" size={28} />
            <div>
              <h1 className="text-3xl font-bold text-nord-6">Upload Artifact</h1>
              <p className="text-sm text-nord-4 mt-1">
                Publish a locally held model or dataset into Harbor and the Data Repository
              </p>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {aborted ? (
          <div className="rounded-xl border border-nord-3 bg-nord-1 p-10 text-center">
            <h2 className="text-lg font-semibold text-nord-6">Upload aborted</h2>
            <p className="text-sm text-nord-4 mt-2">
              The session was cancelled. Any blobs already streamed to Harbor are reclaimed by garbage collection.
            </p>
            <button
              type="button"
              onClick={() => {
                setAborted(false);
                setStep('category');
                setCategory(null);
                setFormat('');
                setEntries([]);
                setSession(null);
              }}
              className="mt-4 rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9"
            >
              Start a new upload
            </button>
          </div>
        ) : (
          <>
            {step !== 'progress' && (
              <nav className="mb-6 flex items-center gap-2 text-sm">
                {STEP_LABELS.slice(0, 3).map((entry, index) => (
                  <div key={entry.key} className="flex items-center gap-2">
                    <span
                      className={cn(
                        'flex h-6 w-6 items-center justify-center rounded-full text-xs font-semibold',
                        index < stepIndex
                          ? 'bg-nord-14 text-nord-0'
                          : index === stepIndex
                            ? 'bg-nord-10 text-nord-6'
                            : 'bg-nord-3 text-nord-4',
                      )}
                    >
                      {index + 1}
                    </span>
                    <span className={cn(index === stepIndex ? 'text-nord-6 font-medium' : 'text-nord-4')}>
                      {entry.label}
                    </span>
                    {index < 2 && <span className="text-nord-4 mx-1">—</span>}
                  </div>
                ))}
              </nav>
            )}

            {step === 'category' && (
              <CategoryStep
                category={category}
                format={format}
                onCategoryChange={(value) => {
                  setCategory(value);
                  if (value === 'model') setFormat('');
                }}
                onFormatChange={setFormat}
                onNext={() => setStep('folder')}
              />
            )}

            {step === 'folder' && (
              <FolderStep
                category={category}
                format={format}
                entries={entries}
                onChange={setEntries}
                onBack={() => setStep('category')}
                onNext={() => setStep('metadata')}
              />
            )}

            {step === 'metadata' && (
              <MetadataStep
                category={category}
                format={format}
                submitting={submitting}
                submitError={submitError}
                onBack={() => setStep('folder')}
                onSubmit={handleSubmit}
              />
            )}

            {step === 'progress' && session && (
              <ProgressStep session={session} entries={entries} onCancel={() => setAborted(true)} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
