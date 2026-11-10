import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Where /api goes in development: the worker, unless CLEA_API says otherwise (`pnpm dev:mock` points it at
// dev/mock-api.mjs). The same variable the desktop app reads.
const api = process.env.CLEA_API || "http://127.0.0.1:4848";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5174, proxy: { "/api": api } },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 900 },
});
