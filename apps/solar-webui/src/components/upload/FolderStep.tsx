import { useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, FileWarning, FolderOpen, Inbox, TriangleAlert } from 'lucide-react';
import { UploadCategory } from '@/api/types';
import { cn, formatBytes } from '@/lib/utils';
import { SelectedUploadFile, datasetWarnings, deriveUploadPaths, modelWarnings } from '@/lib/uploadPaths';

const EXCLUSION_NOTICE = [
  '.git/**',
  '.gitattributes',
  '.gitignore',
  '.DS_Store',
  'Thumbs.db',
  'desktop.ini',
  '__pycache__/**',
  '*.pyc',
  '*.tmp',
  '*.part',
  '*.lock',
  '.ipynb_checkpoints/**',
];

interface FolderStepProps {
  category: UploadCategory | null;
  format: string;
  entries: SelectedUploadFile[];
  onChange: (entries: SelectedUploadFile[]) => void;
  onBack: () => void;
  onNext: () => void;
}

export function FolderStep({ category, format, entries, onChange, onBack, onNext }: FolderStepProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [selectedPaths, setSelectedPaths] = useState<Set<string>>(() => new Set(entries.map((entry) => entry.path)));

  const handleFiles = (files: FileList | null) => {
    if (!files) return;
    const derived = deriveUploadPaths(Array.from(files));
    onChange(derived);
    setSelectedPaths(new Set(derived.map((entry) => entry.path)));
  };

  const togglePath = (path: string) => {
    setSelectedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const selected = entries.filter((entry) => selectedPaths.has(entry.path));
  const totalBytes = selected.reduce((sum, entry) => sum + entry.file.size, 0);
  const warnings = [
    ...(category === 'model' ? modelWarnings(selected) : []),
    ...(category === 'dataset' ? datasetWarnings(selected, format) : []),
  ];

  return (
    <div className="space-y-6">
      <input
        ref={inputRef}
        type="file"
        multiple
        className="hidden"
        onChange={(event) => handleFiles(event.target.files)}
        data-testid="folder-picker"
        // webkitdirectory is not in React's input attribute types.
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
      />

      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        className="flex w-full items-center justify-center gap-3 rounded-xl border-2 border-dashed border-nord-3 bg-nord-1 px-6 py-10 text-nord-4 transition-colors hover:border-nord-10/60 hover:text-nord-6"
      >
        <FolderOpen size={28} />
        <span className="text-base font-medium">
          {entries.length > 0 ? 'Choose a different folder…' : 'Choose a folder'}
        </span>
      </button>

      <div className="rounded-xl border border-nord-3 bg-nord-1 p-5">
        <h4 className="text-sm font-semibold text-nord-6 uppercase tracking-wide">Excluded automatically</h4>
        <p className="text-xs text-nord-4 mt-1">The following are filtered out and never reach the artifact:</p>
        <div className="flex flex-wrap gap-1.5 mt-2">
          {EXCLUSION_NOTICE.map((pattern) => (
            <span key={pattern} className="rounded bg-nord-2 px-2 py-0.5 text-xs text-nord-4 font-mono">
              {pattern}
            </span>
          ))}
        </div>
      </div>

      {warnings.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-nord-13/60 bg-nord-13/10 p-4 text-sm text-nord-13">
          <TriangleAlert size={18} className="mt-0.5 shrink-0" />
          <ul className="space-y-1 list-disc list-inside">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {entries.length > 0 && (
        <div className="rounded-xl border border-nord-3 bg-nord-1 overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3 border-b border-nord-3">
            <h4 className="text-sm font-semibold text-nord-6">
              Files to upload
              <span className="ml-2 text-nord-4 font-normal">
                {selected.length} selected · {formatBytes(totalBytes)}
              </span>
            </h4>
          </div>
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-nord-4 border-b border-nord-3">
                  <th className="px-5 py-2 font-medium w-10"></th>
                  <th className="px-2 py-2 font-medium">Path</th>
                  <th className="px-5 py-2 font-medium text-right">Size</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => {
                  const isSelected = selectedPaths.has(entry.path);
                  return (
                    <tr
                      key={entry.path}
                      className={cn(
                        'border-b border-nord-3 last:border-0',
                        isSelected ? 'text-nord-6' : 'text-nord-4 opacity-60',
                      )}
                    >
                      <td className="px-5 py-2">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => togglePath(entry.path)}
                          aria-label={`Include ${entry.path}`}
                          className="accent-nord-10"
                        />
                      </td>
                      <td className="px-2 py-2 font-mono text-xs">{entry.path}</td>
                      <td className="px-5 py-2 text-right">{formatBytes(entry.file.size)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {entries.length === 0 && (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-nord-3 bg-nord-1 p-8 text-nord-4">
          <Inbox size={28} />
          <p className="text-sm">No folder chosen yet — a directory never appears as a file.</p>
        </div>
      )}

      {selected.length === 0 && entries.length > 0 && (
        <div className="flex items-center gap-2 text-sm text-nord-11">
          <FileWarning size={16} />
          Deselecting every file is allowed, but you cannot continue with an empty artifact.
        </div>
      )}

      <div className="flex justify-between">
        <button
          type="button"
          onClick={onBack}
          className="flex items-center gap-1 rounded-lg border border-nord-3 px-4 py-2 text-sm font-medium text-nord-4 transition-colors hover:bg-nord-2"
        >
          <ChevronLeft size={16} /> Back
        </button>
        <button
          type="button"
          disabled={selected.length === 0}
          onClick={onNext}
          className="flex items-center gap-1 rounded-lg bg-nord-10 px-4 py-2 text-sm font-medium text-nord-6 transition-colors hover:bg-nord-9 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}
