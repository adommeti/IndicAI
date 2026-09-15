---
paths: ["apps/*/ui/**"]
---
# UI rules

- React 18 + Vite + TypeScript + Tailwind, one package per app under `apps/<app>/ui/` with its own
  `package.json`, `npm run build` producing `dist/` served by the app's FastAPI at `/`.
- No generic scaffold look: define a small design token layer (colors, type scale, spacing) in
  `src/styles/tokens.css`, use a consistent 8-pt grid, real empty/loading/error states, keyboard
  navigation, and WCAG AA contrast. Dark and light themes via `prefers-color-scheme`.
- Auth via MSAL (Entra ID) with `VITE_AUTH_DEV_BYPASS=true` for local; roles come from the API,
  never from the client.
- Language-aware: Devanagari/Telugu/Tamil rendering with a font stack that covers all four
  scripts; user preference for Hindi script (Devanagari vs Latn) persisted per user.
- Tests: Vitest for logic, Playwright smoke for the primary flow (skipped in CI unless
  `E2E=1`). `npm run lint` (eslint) and `npm run typecheck` (tsc) are part of `make check` once a
  UI package exists.
