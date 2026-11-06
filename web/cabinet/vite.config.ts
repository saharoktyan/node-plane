import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, type Plugin } from 'vite';

/** In mock mode the dev server hands out MSW's service worker; it never
 * reaches a build. */
function mockWorker(): Plugin {
  const path = createRequire(import.meta.url).resolve('msw/mockServiceWorker.js');
  return {
    name: 'node-plane-mock-worker',
    configureServer(server) {
      server.middlewares.use('/mockServiceWorker.js', (_request, response) => {
        response.setHeader('Content-Type', 'text/javascript');
        response.end(readFileSync(path));
      });
    }
  };
}

export default defineConfig(({ mode }) => ({
  plugins: [sveltekit(), ...(mode === 'mock' ? [mockWorker()] : [])],
  define: { __MOCK__: JSON.stringify(mode === 'mock') },
  server: {
    // Without mocks, a controller's API (or a relay) answers on the same origin.
    proxy: mode === 'mock' ? undefined : { '/api': process.env.NODE_PLANE_API ?? 'http://127.0.0.1:8080' }
  },
  test: { include: ['src/**/*.test.ts', 'scripts/**/*.test.ts'] }
}));
