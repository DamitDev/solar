import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { CategoryStep } from '@/components/upload/CategoryStep';
import { UploadCategory } from '@/api/types';

// The step is controlled — the harness holds the state like ArtifactUpload does.
function Harness() {
  const [category, setCategory] = useState<UploadCategory | null>(null);
  const [format, setFormat] = useState('');
  return (
    <CategoryStep
      category={category}
      format={format}
      onCategoryChange={setCategory}
      onFormatChange={setFormat}
      onNext={vi.fn()}
    />
  );
}

describe('CategoryStep', () => {
  it('renders model requirements', async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole('button', { name: /Model/i }));

    expect(screen.getByText('Model requirements')).toBeInTheDocument();
    expect(screen.getByText(/config\.json \+ weights/)).toBeInTheDocument();
    expect(screen.getByText(/\.gguf files/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Next/ })).toBeEnabled();
  });

  it('renders dataset requirements with a required format select', async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole('button', { name: /Dataset/i }));

    expect(screen.getByText('Dataset requirements')).toBeInTheDocument();
    const select = screen.getByLabelText('Format *');
    expect(select).toBeInTheDocument();
    // No format chosen yet -> cannot continue.
    expect(screen.getByRole('button', { name: /Next/ })).toBeDisabled();

    await userEvent.selectOptions(select, 'parquet');
    expect(screen.getByRole('button', { name: /Next/ })).toBeEnabled();
  });
});
