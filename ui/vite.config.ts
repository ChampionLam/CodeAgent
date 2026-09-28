import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf-8')) as { version?: string };

export default defineConfig({
  // The renderer reads this in 关于 (settings → about). Baked at build time so
  // the UI does not need an IPC round-trip for a constant.
  define: { __APP_VERSION__: JSON.stringify(pkg.version ?? 'dev') },
  // Relative asset URLs: the built bundle is loaded from file:// inside Electron.
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Keep readable names so the Electron shell can point at stable paths.
    assetsDir: 'assets'
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true
  }
});