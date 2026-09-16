import type { ReactNode } from "react";
import { ROLE_LABELS, routeHref } from "../lib/roles";
import type { Capabilities, ViewName } from "../lib/roles";
import { SCRIPT_PREFS } from "../lib/script";
import type { ScriptPref } from "../lib/script";
import type { Me } from "../lib/types";

const VIEW_LABELS: Record<ViewName, string> = {
  queue: "Review queue",
  qa: "QA sample",
  metrics: "Quality & chain",
};

export function Shell({
  me,
  caps,
  view,
  views,
  scriptPref,
  onScriptPref,
  children,
}: {
  me: Me | null;
  caps: Capabilities;
  view: ViewName;
  views: ViewName[];
  scriptPref: ScriptPref;
  onScriptPref: (pref: ScriptPref) => void;
  children: ReactNode;
}) {
  return (
    <div className="min-h-screen">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <div className="mr-auto">
            <h1 className="text-lg font-semibold leading-tight">Communication surveillance</h1>
            <p className="text-xs text-muted">Reviewer console · uc3</p>
          </div>

          {/* The script choice only exists where transcript text does. */}
          {caps.queue ? (
            <fieldset className="flex items-center gap-2">
              <legend className="sr-only">Transcript script</legend>
              <span className="text-xs uppercase tracking-wider text-muted">Script</span>
              <div className="flex rounded border border-border" role="group">
                {SCRIPT_PREFS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => onScriptPref(option.value)}
                    aria-pressed={scriptPref === option.value}
                    title={option.hint}
                    className={`px-3 py-1 text-xs first:rounded-l last:rounded-r ${
                      scriptPref === option.value
                        ? "bg-accent text-[color:var(--accent-ink)] font-semibold"
                        : "text-muted"
                    }`}
                    data-testid={`script-${option.value}`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </fieldset>
          ) : null}

          <div className="text-right">
            <p className="font-mono text-xs" data-testid="identity">
              {me ? me.identity : "…"}
            </p>
            <p className="text-xs text-muted" data-testid="roles">
              {me && me.roles.length > 0
                ? me.roles.map((role) => ROLE_LABELS[role] ?? role).join(" · ")
                : "no role assigned"}
            </p>
          </div>
        </div>

        {me?.dev_bypass ? (
          <p
            className="border-t border-warn-edge bg-warn-wash px-4 py-1 text-center text-xs font-semibold text-warn"
            data-testid="dev-bypass"
          >
            Dev bypass — identity and roles are fixed test values supplied by the API, not a signed-in
            user. The service refuses this mode in production.
          </p>
        ) : null}

        {views.length > 1 ? (
          <nav aria-label="Views" className="mx-auto max-w-[1600px] px-4">
            <ul className="-mb-px flex gap-1">
              {views.map((name) => (
                <li key={name}>
                  <a
                    href={routeHref(name)}
                    aria-current={view === name ? "page" : undefined}
                    data-testid={`nav-${name}`}
                    className={`inline-block border-b-2 px-3 py-2 text-sm ${
                      view === name
                        ? "border-accent font-semibold text-ink"
                        : "border-transparent text-muted hover:text-ink"
                    }`}
                  >
                    {VIEW_LABELS[name]}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        ) : null}
      </header>

      <main id="main" className="mx-auto max-w-[1600px] px-4 py-4">
        {children}
      </main>
    </div>
  );
}
