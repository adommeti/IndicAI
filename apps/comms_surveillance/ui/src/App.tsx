import { useCallback, useEffect, useMemo, useState } from "react";
import { Shell } from "./components/Shell";
import { ErrorPanel, Loading, RoleNotice } from "./components/States";
import { api } from "./lib/api";
import { allowedViews, capabilities, defaultView, parseRoute, routeHref } from "./lib/roles";
import type { ViewName } from "./lib/roles";
import { loadScriptPref, saveScriptPref } from "./lib/script";
import type { ScriptPref } from "./lib/script";
import type { Me } from "./lib/types";
import { useAsync } from "./lib/useAsync";
import { MetricsView } from "./views/MetricsView";
import { QaView } from "./views/QaView";
import { QueueView } from "./views/QueueView";

function useHashRoute() {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return useMemo(() => parseRoute(hash), [hash]);
}

const VIEW_SUBJECTS: Record<ViewName, string> = {
  queue: "the review queue, call transcripts and recordings",
  qa: "the random QA sample",
  metrics: "detection quality metrics and audit-chain verification",
};

export default function App() {
  const loadMe = useCallback(() => api.me(), []);
  const { data: me, error, loading, reload } = useAsync<Me>(loadMe);
  const route = useHashRoute();

  // Roles come from the API and from nowhere else: no token is parsed, no role is
  // read from the URL, and nothing here grants anything. The service enforces
  // every rule and answers 403 on its own account (.claude/rules/apps.md).
  const caps = useMemo(() => capabilities(me?.roles ?? []), [me]);
  const views = useMemo(() => allowedViews(caps), [caps]);

  const [scriptPref, setScriptPref] = useState<ScriptPref>("native");
  useEffect(() => {
    if (me) setScriptPref(loadScriptPref(me.identity));
  }, [me]);

  const onScriptPref = useCallback(
    (pref: ScriptPref) => {
      setScriptPref(pref);
      if (me) saveScriptPref(me.identity, pref);
    },
    [me],
  );

  // Land the user on a view their role actually has endpoints for, rather than
  // leaving them on a panel that can only ever be a refusal.
  const fallback = defaultView(caps);
  useEffect(() => {
    if (!me || !fallback) return;
    if (!views.includes(route.view)) window.location.hash = routeHref(fallback);
  }, [me, fallback, views, route.view]);

  if (loading)
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Loading label="Signing in" rows={3} />
      </div>
    );

  if (error || !me)
    return (
      <div className="mx-auto max-w-2xl p-8">
        <ErrorPanel error={error ?? new Error("No identity returned")} onRetry={reload} />
      </div>
    );

  return (
    <Shell
      me={me}
      caps={caps}
      view={route.view}
      views={views}
      scriptPref={scriptPref}
      onScriptPref={onScriptPref}
    >
      {views.length === 0 ? (
        <RoleNotice what="any view in this console" roles={me.roles} />
      ) : !views.includes(route.view) ? (
        <RoleNotice what={VIEW_SUBJECTS[route.view]} roles={me.roles} />
      ) : route.view === "queue" ? (
        <QueueView flagId={route.flagId} scriptPref={scriptPref} roles={me.roles} />
      ) : route.view === "qa" ? (
        <QaView scriptPref={scriptPref} roles={me.roles} />
      ) : (
        <MetricsView roles={me.roles} governanceOnly={!caps.queue} />
      )}
    </Shell>
  );
}
