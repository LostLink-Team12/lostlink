import { defineConfig, loadEnv } from 'vite';
import { resolve } from 'node:path';
import { devProxy } from './src/config';

export default defineConfig(({ mode }) => ({
  server: { proxy: devProxy({ ...loadEnv(mode, process.cwd(), ''), ...process.env }) },
  build: { rollupOptions: { input: {
    owner: resolve('owner-app/index.html'), venue: resolve('venue-portal/index.html'),
  } } },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.js'] },
}));
