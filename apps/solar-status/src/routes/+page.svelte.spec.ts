import { render, screen } from '@testing-library/svelte';
import { expect, it } from 'vitest';
import Page from './+page.svelte';

it('renders the status page heading', () => {
	render(Page);

	expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Solar status');
});
