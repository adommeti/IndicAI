import type { ReactNode } from "react";
import type { AuthMode } from "../lib/auth";
import { roleLabel, routeHref } from "../lib/roles";
import type { ViewName } from "../lib/roles";
import { LANGUAGES, SCRIPT_PREFS } from "../lib/script";
import type { ScriptPref, SpokenLanguage } from "../lib/script";
import type { Me } from "../lib/types";

const VIEW_LABELS: Record<ViewName, string> = {
  chat: "Helpdesk",
  replay: "Session replay",
};

export function Shell({
  me,
  mode,
  view,
  views,
  language,
  onLanguage,
  scriptPref,
  onScriptPref,
  bypassRefused,
  children,
}: {
  me: Me | null;
  mode: AuthMode;
  view: ViewName;
  views: ViewName[];
  language: SpokenLanguage;
  onLanguage: (language: SpokenLanguage) => void;
  scriptPref: ScriptPref;
  onScriptPref: (pref: ScriptPref) => void;
  bypassRefused: boolean;
  children: ReactNode;
}) {
  return (
    <div className="min-h-screen">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="border-b border-border bg-surface">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <div className="mr-auto">
            <h1 className="text-lg font-semibold leading-tight">Employee helpdesk</h1>
            <p className="text-xs text-muted">Voice and chat · uc1</p>
          </div>

          <fieldset className="flex items-center gap-2">
            <legend className="sr-only">Conversation language</legend>
            <span className="text-xs uppercase tracking-wider text-muted">Language</span>
            <div className="flex rounded border border-border" role="group">
              {LANGUAGES.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => onLanguage(option.value)}
                  aria-pressed={language === option.value}
                  title={option.english}
                  data-testid={`language-${option.value}`}
                  className={`indic px-3 py-1 text-xs first:rounded-l last:rounded-r ${
                    language === option.value
                      ? "bg-accent font-semibold text-[color:var(--accent-ink)]"
                      : "text-muted hover:text-ink"
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </fieldset>

          {/* Hindi is the only language with two scripts here, so the toggle exists
              only where it means something rather than sitting inert for Tamil. */}
          {language === "hi" ? (
            <fieldset className="flex items-center gap-2" data-testid="script-toggle">
              <legend className="sr-only">Hindi script</legend>
              <span className="text-xs uppercase tracking-wider text-muted">Script</span>
              <div className="flex rounded border border-border" role="group">
                {SCRIPT_PREFS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => onScriptPref(option.value)}
                    aria-pressed={scriptPref === option.value}
                    title={option.hint}
                    data-testid={`script-${option.value}`}
                    className={`indic px-3 py-1 text-xs first:rounded-l last:rounded-r ${
                      scriptPref === option.value
                        ? "bg-accent font-semibold text-[color:var(--accent-ink)]"
                        : "text-muted hover:text-ink"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </fieldset>
          ) : null}

          <div className="text-right">
            <p className="font-mono text-xs" data-testid="identity">
              {me ? me.employee_id : "…"}
            </p>
            <p className="text-xs text-muted" data-testid="roles">
              {me && me.roles.length > 0 ? me.roles.map(roleLabel).join(" · ") : "no role assigned"}
            </p>
          </div>
        </div>

        {mode.kind === "dev-bypass" ? (
          <p
            className="border-t border-warn-edge bg-warn-wash px-4 py-1 text-center text-xs font-semibold text-warn"
            data-testid="dev-bypass"
          >
            Development sign-in bypass — this page acquires no Entra ID token. Identity and roles
            still come from the service, which has no bypass of its own.
          </p>
        ) : null}

        {bypassRefused ? (
          <p
            className="border-t border-danger-edge bg-danger-wash px-4 py-1 text-center text-xs font-semibold text-danger"
            data-testid="bypass-refused"
          >
            This bundle was built with the development sign-in bypass, which is refused off a
            loopback origin. It is signing nobody in. Rebuild without VITE_AUTH_DEV_BYPASS.
          </p>
        ) : null}

        {views.length > 1 ? (
          <nav aria-label="Views" className="mx-auto max-w-[1400px] px-4">
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

      <main id="main" className="mx-auto max-w-[1400px] px-4 py-4">
        {children}
      </main>
    </div>
  );
}
