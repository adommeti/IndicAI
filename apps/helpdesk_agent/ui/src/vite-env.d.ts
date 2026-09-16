/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Same-origin by default: the FastAPI app serves this bundle from "/". */
  readonly VITE_API_BASE?: string;
  /** Local development only. Honoured only on a loopback origin, and it signs
   *  nobody in: roles still come from GET /me and every rule is server-side.
   *  See src/lib/auth.ts for exactly what it does and cannot do. */
  readonly VITE_AUTH_DEV_BYPASS?: string;
  /** Entra ID application (client) id of THIS single-page app. */
  readonly VITE_MSAL_CLIENT_ID?: string;
  /** Entra tenant id (a GUID) or `organizations` / `common`. */
  readonly VITE_MSAL_TENANT?: string;
  /** Full authority URL. Overrides VITE_MSAL_TENANT when a sovereign or B2C
   *  cloud is in play, where https://login.microsoftonline.com is wrong. */
  readonly VITE_MSAL_AUTHORITY?: string;
  /** The API's exposed scope, e.g. `api://<api-client-id>/access_as_user`.
   *  Without it MSAL can only get a token for Graph, which this API will not
   *  accept, so a missing value is a configuration error and not a default. */
  readonly VITE_API_SCOPE?: string;
  /** Path that mints a LiveKit join token for the signed-in employee. Empty by
   *  default because the uc1/P6 API contract has no such endpoint yet — see
   *  src/lib/voice.ts. */
  readonly VITE_LIVEKIT_TOKEN_PATH?: string;
  /** LiveKit websocket URL, when the token endpoint does not return one. */
  readonly VITE_LIVEKIT_URL?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
