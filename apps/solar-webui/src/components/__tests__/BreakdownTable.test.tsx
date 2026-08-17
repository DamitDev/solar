import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { BreakdownRow, BreakdownTable } from '../gateway/BreakdownTable';

const rows: BreakdownRow[] = [
  {
    id: 'b',
    label: 'qwen3.6:35b',
    completed: 5,
    token_in: 900,
    token_cached: 600,
    token_out: 100,
    avg_duration_s: 12.5,
  },
  {
    id: 'a',
    label: 'iris-bert:110m',
    completed: 50,
    token_in: 100,
    token_cached: 0,
    token_out: 50,
    avg_duration_s: 0.09,
  },
  {
    id: 'c',
    label: 'solver-v4:9b',
    completed: 12,
    token_in: 500,
    token_cached: 200,
    token_out: 400,
    avg_duration_s: 24.61,
  },
];

function labelColumn(): string[] {
  const body = screen.getAllByRole('rowgroup')[1];
  return within(body)
    .getAllByRole('row')
    .map((row) => within(row).getAllByRole('cell')[0].textContent ?? '');
}

describe('BreakdownTable', () => {
  it('defaults to alphabetical order by label', () => {
    render(<BreakdownTable title="By Model" labelHeading="Model" rows={rows} />);

    expect(labelColumn()).toEqual(['iris-bert:110m', 'qwen3.6:35b', 'solver-v4:9b']);
  });

  it('marks the active sort column for assistive tech', () => {
    render(<BreakdownTable title="By Model" labelHeading="Model" rows={rows} />);

    expect(screen.getByRole('columnheader', { name: /Model/ })).toHaveAttribute('aria-sort', 'ascending');
    expect(screen.getByRole('columnheader', { name: /Completed/ })).toHaveAttribute('aria-sort', 'none');
  });

  it('reverses the label order when the same header is clicked twice', async () => {
    const user = userEvent.setup();
    render(<BreakdownTable title="By Model" labelHeading="Model" rows={rows} />);

    await user.click(screen.getByRole('button', { name: /Model/ }));

    expect(labelColumn()).toEqual(['solver-v4:9b', 'qwen3.6:35b', 'iris-bert:110m']);
  });

  it('sorts a numeric column from the largest value down on first click', async () => {
    const user = userEvent.setup();
    render(<BreakdownTable title="By Model" labelHeading="Model" rows={rows} />);

    await user.click(screen.getByRole('button', { name: /Completed/ }));

    expect(labelColumn()).toEqual(['iris-bert:110m', 'solver-v4:9b', 'qwen3.6:35b']);
  });

  it('sorts by average duration', async () => {
    const user = userEvent.setup();
    render(<BreakdownTable title="By Model" labelHeading="Model" rows={rows} />);

    await user.click(screen.getByRole('button', { name: /Avg Duration/ }));

    expect(labelColumn()).toEqual(['solver-v4:9b', 'qwen3.6:35b', 'iris-bert:110m']);
  });

  it('summarises row count and completed total', () => {
    render(<BreakdownTable title="By Host" labelHeading="Host" rows={rows} />);

    expect(screen.getByText('3 rows • 67 completed')).toBeInTheDocument();
  });

  it('shows an empty state and no summary without rows', () => {
    render(<BreakdownTable title="By Host" labelHeading="Host" rows={[]} />);

    expect(screen.getByText('No data')).toBeInTheDocument();
    expect(screen.queryByText(/completed/)).not.toBeInTheDocument();
  });
});
