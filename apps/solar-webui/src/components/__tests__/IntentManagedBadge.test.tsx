import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { IntentManagedBadge } from '@/components/IntentManagedBadge';

const managedInstance = {
  id: 'i1',
  config: { alias: 'iris:v1', managed_by: 'intent', intent_id: 'intent-1' },
  status: 'running',
};

const manualInstance = {
  id: 'i2',
  config: { alias: 'scratch:v1' },
  status: 'running',
};

function renderBadge(instance: any) {
  return render(
    <MemoryRouter>
      <IntentManagedBadge instance={instance} />
    </MemoryRouter>,
  );
}

describe('IntentManagedBadge', () => {
  it('renders "managed" for an intent-managed instance', () => {
    renderBadge(managedInstance);
    expect(screen.getByText('managed')).toBeInTheDocument();
  });

  it('renders nothing for a manual instance', () => {
    const { container } = renderBadge(manualInstance);
    expect(container).toBeEmptyDOMElement();
  });

  it('never renders an internal issue reference', () => {
    const { container } = renderBadge(managedInstance);
    expect(container.textContent).not.toMatch(/[SDNU]-\d{3}/);
  });
});
