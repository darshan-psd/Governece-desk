// Development server for the React app (`pnpm dev`), with hot module reload.
//
// Production is unchanged: `pnpm frontend:build` bundles with esbuild into .sites-runtime/frontend,
// which the FastAPI backend serves. This config exists only so the frontend can be edited without
// rebuilding; it never produces the deployed assets.
//
// The browser talks only to this server (127.0.0.1:5173) and every /api request is proxied to the
// backend, so the two stay same-origin and the session cookies (SameSite=Strict, no Domain) keep
// working. Use 127.0.0.1, not localhost: they are separate cookie origins.
import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

const API = process.env.GOVERNANCE_API_URL || 'http://ec2-13-201-73-29.ap-south-1.compute.amazonaws.com:8005';
const origin = new URL(API).origin;

// app.core.security.LocalSecurityMiddleware rejects an unrecognised Host (DNS-rebinding guard) and
// any cross-site Origin. changeOrigin rewrites Host to the backend's; Origin is rewritten here, so
// the backend sees a request from its own origin instead of the dev server's port.
const api = {
  target: API,
  changeOrigin: true,
  configure: (proxy: {on: (event: string, handler: (request: {getHeader(name: string): unknown; setHeader(name: string, value: string): void}) => void) => void}) => {
    proxy.on('proxyReq', request => {
      if (request.getHeader('origin')) request.setHeader('origin', origin);
    });
  },
};

export default defineConfig({
  root: 'frontend',
  // The production build copies globals.css verbatim, so no PostCSS/Tailwind pass runs here either
  // (an empty plugin list also stops Vite picking up the root postcss.config.mjs).
  css: {postcss: {plugins: []}},
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 8005,
    strictPort: true,
    proxy: {'/api': api, '/health': api, '/ready': api},
  },
});
