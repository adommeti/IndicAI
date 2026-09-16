import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** The API serves this bundle from "/" in production, so the default API base is the
 *  empty string and every path in the pinned uc1/P6 contract -- /me, /chat/turn,
 *  /sessions/{id}/replay -- is same-origin. In `npm run dev` those exact prefixes are
 *  proxied to the local FastAPI instead. */
const API_PREFIXES = ["/me", "/chat", "/sessions", "/health"];

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 is Vite's default and 5174 is uc3's; uc1 takes the next one so all three
    // UIs can run at once against one API.
    port: 5175,
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
