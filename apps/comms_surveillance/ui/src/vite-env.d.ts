/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Same-origin by default: the FastAPI app serves this bundle from "/". */
  readonly VITE_API_BASE?: string;
  /** Local development against a dev-bypass API. Never enables anything in the
   *  browser: roles still come from GET /me and every rule is enforced server-side. */
  readonly VITE_AUTH_DEV_BYPASS?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
