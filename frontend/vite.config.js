import { defineConfig, loadEnv } from 'vite';
import { resolve } from 'node:path';
import { devProxy } from './src/config';

export default defineConfig(({ mode }) => ({
  plugins: [{ name: 'match-deep-links', configureServer(server) { server.middlewares.use((req, _res, next) => { if (/^\/matches\/[^/?]+\/?(?:\?.*)?$/.test(req.url || '')) req.url = '/matches/index.html'; next(); }); } }],
  server: { proxy: devProxy({ ...loadEnv(mode, process.cwd(), ''), ...process.env }) },
  build: { rollupOptions: { input: {
    matches: resolve('matches/index.html'), owner: resolve('owner-app/index.html'), venue: resolve('venue-portal/index.html'),
  } } },
  test: { environment: 'jsdom', setupFiles: ['./tests/setup.js'] },
}));
