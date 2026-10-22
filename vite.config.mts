// Development server for the React app (`pnpm dev`), with hot module reload.

//

// Production is unchanged: `pnpm frontend:build` bundles with esbuild into .sites-runtime/frontend,

// which the FastAPI backend serves. This config exists only so the frontend can be edited without

// rebuilding; it never produces the deployed assets.

//

// The browser talks only to this dev server and every /api request is proxied to the backend,

// so the two stay same-origin and the session cookies (SameSite=Strict, no Domain) keep working.

// Locally use 127.0.0.1, not localhost: they are separate cookie origins.

import {defineConfig} from 'vite';

import react from '@vitejs/plugin-react';
 
const API = process.env.GOVERNANCE_API_URL || 'http://ec2-13-201-73-29.ap-south-1.compute.amazonaws.com:8005';

const origin = new URL(API).origin;
 
// Port the dev server listens on. Must differ from the backend's port if both run on the same machine.

const DEV_PORT = Number(process.env.DEV_PORT || 8005);
 
// Hosts the dev server will accept in the Host header (Vite's DNS-rebinding guard).

// The leading dot allows the domain and all its subdomains, so it survives EC2 hostname changes.

const ALLOWED_HOSTS = ['http://ec2-13-201-73-29.ap-south-1.compute.amazonaws.com', 'localhost', '127.0.0.1'];
 
// app.core.security.LocalSecurityMiddleware rejects an unrecognised Host (DNS-rebinding guard) and

// any cross-site Origin. changeOrigin rewrites Host to the backend's; Origin is rewritten here, so

// the backend sees a request from its own origin instead of the dev server's.

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

    port: DEV_PORT,

    strictPort: true,

    allowedHosts: ALLOWED_HOSTS,

    proxy: {'/api': api, '/health': api, '/ready': api},

  },

});
 