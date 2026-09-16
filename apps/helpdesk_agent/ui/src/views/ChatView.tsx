import { useCallback, useRef, useState } from "react";
import { EmptyState, ErrorPanel } from "../components/States";
import { api } from "../lib/api";
import type { AuthMode } from "../lib/auth";
import { tagLabel } from "../lib/script";
import type { Action, LanguageTag, Ticket } from "../lib/types";
import { VoicePanel } from "./VoicePanel";

interface Entry {
  id: string;
  who: "employee" | "agent";
  text: string;
  language: LanguageTag;
  action?: Action;
  citations?: string[];
  ticket?: Ticket | null;
  ticketId?: string | null;
}

const ACTION_LABELS: Record<Action, string> = {
  answer: "Answered from the knowledge base",
  clarify: "Needs more detail",
  file_ticket: "Ticket",
};

export function ChatView({ language, mode }: { language: LanguageTag; mode: AuthMode }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const sessionId = useRef<string | null>(null);
  const seq = useRef(0);

  const send = useCallback(async () => {
    const utterance = draft.trim();
    if (!utterance || pending) return;
    seq.current += 1;
    const mine: Entry = { id: `e${seq.current}`, who: "employee", text: utterance, language };
    setEntries((current) => [...current, mine]);
    setDraft("");
    setPending(true);
    setError(null);
    try {
      const body = await api.chatTurn({
        utterance,
        language,
        ...(sessionId.current ? { session_id: sessionId.current } : {}),
      });
      sessionId.current = body.session_id;
      seq.current += 1;
      setEntries((current) => [
        ...current,
        {
          id: `a${seq.current}`,
          who: "agent",
          text: body.decision.reply_text,
          language,
          action: body.decision.action,
          citations: body.decision.cited_article_ids,
          ticket: body.decision.ticket,
          ticketId: body.ticket_id,
        },
      ]);
    } catch (cause) {
      setError(cause);
    } finally {
      setPending(false);
    }
  }, [draft, language, pending]);

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <VoicePanel language={language} devBypass={mode.kind === "dev-bypass"} />

      <section
        aria-labelledby="chat-heading"
        className="rounded border border-border bg-surface p-4"
        data-testid="chat-panel"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="chat-heading" className="text-base font-semibold">
            Type to the helpdesk
          </h2>
          <p className="text-xs text-muted" data-testid="chat-language">
            Replies in {tagLabel(language)}
          </p>
        </div>

        <ol className="mt-3 space-y-3" data-testid="chat-thread">
          {entries.length === 0 && !pending ? (
            <li>
              <EmptyState
                title="Nothing asked yet"
                body="Describe the problem in your own words — VPN, a password that will not reset, a broken desk phone. You can speak instead, on the left."
              />
            </li>
          ) : null}
          {entries.map((entry) => (
            <li
              key={entry.id}
              data-testid={`chat-${entry.who}`}
              className={`rounded border p-3 ${
                entry.who === "employee"
                  ? "border-border bg-surface-2"
                  : "border-accent-edge bg-surface"
              }`}
            >
              <p className="text-xs uppercase tracking-wider text-muted">
                {entry.who === "employee" ? "You" : "Helpdesk agent"}
              </p>
              <p className="indic mt-1">{entry.text}</p>

              {entry.action ? (
                <p className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                  <span
                    className="rounded border border-border px-2 py-0.5 font-semibold"
                    data-testid="chat-action"
                  >
                    {ACTION_LABELS[entry.action]}
                  </span>
                  {entry.citations && entry.citations.length > 0 ? (
                    <span className="text-muted" data-testid="chat-citations">
                      from {entry.citations.join(", ")}
                    </span>
                  ) : null}
                </p>
              ) : null}

              {entry.ticket ? (
                <div className="mt-2 rounded border border-border bg-surface-2 p-2 text-xs">
                  <p className="font-semibold">{entry.ticket.title}</p>
                  <p className="mt-1 text-muted">{entry.ticket.description}</p>
                  <p className="mt-1 text-muted">
                    {entry.ticket.category} · {entry.ticket.urgency} urgency ·{" "}
                    {entry.ticketId ? (
                      <span data-testid="ticket-id">ticket {entry.ticketId}</span>
                    ) : (
                      // Null is not "no ticket wanted": the decision asked for one and
                      // the filing has not come back. Saying "filed" here would be a
                      // promise this page cannot keep.
                      <span data-testid="ticket-pending">not filed yet</span>
                    )}
                  </p>
                </div>
              ) : null}
            </li>
          ))}
          {pending ? (
            <li
              role="status"
              aria-live="polite"
              className="rounded border border-border bg-surface-2 p-3 text-sm text-muted"
              data-testid="chat-pending"
            >
              The agent is reading the knowledge base…
            </li>
          ) : null}
        </ol>

        {error ? (
          <div className="mt-3">
            <ErrorPanel error={error} onRetry={() => setError(null)} mode={mode} />
          </div>
        ) : null}

        <form
          className="mt-4 space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          <label htmlFor="utterance" className="text-xs uppercase tracking-wider text-muted">
            Your question
          </label>
          <textarea
            id="utterance"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Enter sends, Shift+Enter is a newline: this is a conversation box,
              // not a document editor.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            rows={3}
            maxLength={16000}
            data-testid="utterance"
            className="indic w-full rounded border border-border bg-surface-2 p-2"
            placeholder="…"
          />
          <button
            type="submit"
            disabled={pending || !draft.trim()}
            data-testid="send"
            className="rounded border border-border-strong bg-accent px-4 py-2 text-sm font-semibold text-[color:var(--accent-ink)] disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-2 disabled:text-muted"
          >
            Send
          </button>
        </form>
      </section>
    </div>
  );
}
