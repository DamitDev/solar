import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';
import adapter from '@sveltejs/adapter-static';
import { sveltekit } from '@sveltejs/kit/vite';
import { svelteTesting } from '@testing-library/svelte/vite';

export default defineConfig(({ command }) => {
  const embedded = command === 'build';
  const statusMount = '/status';

  return {
    server: {
      port: 5174
    },
    define: {
      __BASE_PATH__: JSON.stringify(embedded ? statusMount : '/')
    },
    plugins: [
      tailwindcss(),
      sveltekit({
        paths: { base: embedded ? statusMount : '' },
        compilerOptions: {
          // Force runes mode for the project, except for libraries. Can be removed in svelte 6.
          runes: ({ filename }) =>
            filename.split(/[/\\]/).includes('node_modules') ? undefined : true
        },
        adapter: adapter({
          pages: 'dist',
          assets: 'dist',
          fallback: 'index.html'
        })
      }),
      svelteTesting()
    ],
    test: {
      expect: { requireAssertions: true },
      environment: 'jsdom',
      setupFiles: ['src/test/setup.ts'],
      env: { VITE_API_BASE_URL: '../api' }
    }
  };
});
