// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Strict CSP: no inline scripts or styles, no data: URIs for assets, no CDNs.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    // Hashed bundles under /static/ (served by app/core/spa.py): /assets is the
    // Assets page, a client route.
    assetsDir: "static",
    emptyOutDir: true,
    assetsInlineLimit: 0,
    sourcemap: false,
    modulePreload: { polyfill: false },
    cssCodeSplit: true,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});
