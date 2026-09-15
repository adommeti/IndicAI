import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** The API serves this bundle from "/" in production (api.py mounts ui/dist last),
 *  so the default API base is the empty string and every path in the contract --
 *  /me, /flags, /qa-sample, /metrics/*, /audit/* -- is same-origin. In `npm run dev`
 *  those exact prefixes are proxied to the local FastAPI instead. */
const API_PREFIXES = ["/me", "/flags", "/qa-sample", "/metrics", "/audit"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: Object.fromEntries(
      API_PREFIXES.map((prefix) => [
        prefix,
        { target: process.env.VITE_DEV_API ?? "http://localhost:8000", changeOrigin: true },
      ]),
    ),
  },
  build: { outDir: "dist", sourcemap: true },
  test: { environment: "jsdom", globals: true, include: ["tests/**/*.test.ts?(x)"] },
});
