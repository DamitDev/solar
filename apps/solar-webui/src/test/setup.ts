import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// RTL auto-cleanup only registers with vitest globals enabled; without
// globals, DOM from previous tests leaks into the next one.
afterEach(cleanup);
