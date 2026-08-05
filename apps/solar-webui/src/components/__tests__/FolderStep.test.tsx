import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { FolderStep } from '@/components/upload/FolderStep';
import { SelectedUploadFile } from '@/lib/uploadPaths';

function fakeFile(name: string, webkitRelativePath: string): File {
  return Object.assign(new File(['x'], name), { webkitRelativePath });
}

function pickFolder(files: File[]) {
  const input = screen.getByTestId('folder-picker');
  fireEvent.change(input, { target: { files } });
}

// The step is controlled — the harness holds the entries like ArtifactUpload does.
function Harness({
  category,
  format,
  initialEntries = [],
}: {
  category: 'model' | 'dataset';
  format: string;
  initialEntries?: SelectedUploadFile[];
}) {
  const [entries, setEntries] = useState<SelectedUploadFile[]>(initialEntries);
  return (
    <FolderStep
      category={category}
      format={format}
      entries={entries}
      onChange={setEntries}
      onBack={vi.fn()}
      onNext={vi.fn()}
    />
  );
}

describe('FolderStep', () => {
  it('warns on a model with no config.json and no gguf (warning, not a block)', () => {
    render(<Harness category="model" format="" />);

    pickFolder([fakeFile('random.bin', 'my-model/random.bin')]);

    expect(screen.getByText(/Solar Host will not be able to serve this artifact/)).toBeInTheDocument();
    // A warning is not a hard block: the file is still selectable.
    expect(screen.getByRole('button', { name: /Next/ })).toBeEnabled();
  });

  it('warns when no file matches the declared dataset format', () => {
    const entries: SelectedUploadFile[] = [{ file: fakeFile('data.csv', 'ds/data.csv'), path: 'data.csv' }];
    render(<Harness category="dataset" format="parquet" initialEntries={entries} />);

    expect(screen.getByText(/No file matches the declared parquet format/)).toBeInTheDocument();
  });

  it('shows the exclusion list and selected totals', () => {
    const entries: SelectedUploadFile[] = [
      { file: fakeFile('a.bin', 'm/a.bin'), path: 'a.bin' },
      { file: fakeFile('b.bin', 'm/b.bin'), path: 'b.bin' },
    ];
    render(<Harness category="model" format="" initialEntries={entries} />);

    expect(screen.getByText('Excluded automatically')).toBeInTheDocument();
    expect(screen.getByText('.DS_Store')).toBeInTheDocument();
    expect(screen.getByText(/2 selected/)).toBeInTheDocument();
  });

  it('blocks continuing when every file is deselected', () => {
    const entries: SelectedUploadFile[] = [{ file: fakeFile('a.bin', 'm/a.bin'), path: 'a.bin' }];
    render(<Harness category="model" format="" initialEntries={entries} />);

    const checkbox = screen.getByRole('checkbox', { name: /Include a\.bin/ });
    fireEvent.click(checkbox);

    expect(screen.getByRole('button', { name: /Next/ })).toBeDisabled();
  });
});
