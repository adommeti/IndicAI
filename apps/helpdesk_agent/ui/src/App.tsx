import { useCallback, useEffect, useMemo, useState } from "react";
import { Shell } from "./components/Shell";
import { ErrorPanel, Loading } from "./components/States";
import { api, setAuthHeaders } from "./lib/api";
import { bypassRefusedHere, resolveAuth } from "./lib/auth";
import { authHeader } from "./lib/msal";
import { allowedViews, capabilities, parseRoute, routeHref } from "./lib/roles";
import { languageTag, loadScriptPref, saveScriptPref } from "./lib/script";
import type { ScriptPref, SpokenLanguage } from "./lib/script";
import type { Me } from "./lib/types";
import { useAsync } from "./lib/useAsync";
import { ChatView } from "./views/ChatView";
import { ReplayView } from "./views/ReplayView";

function useHashRoute() {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return useMemo(() => parseRoute(hash), [hash]);
}

export default function App() {
  // Decided once, from the build's variables and this origin. Everything about
  // sign-in follows from it, and nothing at runtime can change it.
  const mode = useMemo(() => resolveAuth(import.meta.env, window.location.hostname), []);
  const bypassRefused = useMemo(
    () => bypassRefusedHere(import.meta.env, window.location.hostname),
    [],
  );

  useEffect(() => {
    setAuthHeaders(() => authHeader(mode));
  }, [mode]);

  const loadMe = useCallback(() => api.me(), []);
  const { data: me, error, loading, reload } = useAsync<Me>(loadMe, mode.kind !== "unconfigured");
  const route = useHashRoute();

  // Roles come from the API and from nowhere else: no token is parsed, no role is
  // read from the URL, and nothing here grants anything. The service enforces every
  // rule and answers 403 on its own account (`.claude/rules/apps.md`).
  const caps = useMemo(() => capabilities(me?.roles ?? []), [me]);
  const views = useMemo(() => allowedViews(caps), [caps]);

  const [language, setLanguage] = useState<SpokenLanguage>("hi");
  const [scriptPref, setScriptPref] = useState<ScriptPref>("deva");
  useEffect(() => {
    if (me) setScriptPref(loadScriptPref(me.employee_id));
  }, [me]);

  const onScriptPref = useCallback(
    (pref: ScriptPref) => {
      setScriptPref(pref);
      // Per user: keyed by the subject the API reported, so a shared helpdesk
      // workstation does not hand the next employee this one's preference.
      if (me) saveScriptPref(me.employee_id, pref);
    },
    [me],
  );

  const tag = languageTag(language, scriptPref);

  if (mode.kind === "unconfigured")
    return (
      <div className="mx-auto max-w-2xl p-8">
        <ErrorPanel error={new Error(mode.reason)} mode={mode} />
      </div>
    );

  if (loading)
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Loading label="Signing in" rows={3} />
      </div>
    );

  if (error || !me)
    return (
      <div className="mx-auto max-w-2xl p-8">
        <ErrorPanel
          error={error ?? new Error("No identity returned")}
          onRetry={reload}
          mode={mode}
        />
      </div>
    );

  return (
    <Shell
      me={me}
      mode={mode}
      view={route.view}
      views={views}
      language={language}
      onLanguage={setLanguage}
      scriptPref={scriptPref}
      onScriptPref={onScriptPref}
      bypassRefused={bypassRefused}
    >
      {route.view === "replay" ? (
        <>
          {/* Deliberately NOT gated on `caps.replay`. The tab is hidden without
              the role, but this URL still works and the server still answers —
              hiding a control is not access control, and this is the line where
              that is visible. A caller without the role gets the service's 403,
              rendered as a role notice. */}
          <ReplayView sessionId={route.sessionId} roles={me.roles} mode={mode} />
          <p className="mt-6 text-xs text-muted">
            <a href={routeHref("chat")} className="underline">
              Back to the helpdesk
            </a>
          </p>
        </>
      ) : (
        <ChatView language={tag} mode={mode} />
      )}
    </Shell>
  );
}
