/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // src/styles/tokens.css is the single source of truth for colour; Tailwind
      // only names the variables so a value is never written down twice.
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        border: "var(--border)",
        "border-strong": "var(--border-strong)",
        ink: "var(--ink)",
        muted: "var(--ink-muted)",
        accent: "var(--accent)",
        "accent-ink": "var(--accent-ink)",
        danger: "var(--danger)",
        warn: "var(--warn)",
        ok: "var(--ok)",
        high: "var(--sev-high)",
        medium: "var(--sev-medium)",
        low: "var(--sev-low)",
        evidence: "var(--evidence)",
        "danger-wash": "var(--danger-wash)",
        "danger-edge": "var(--danger-edge)",
        "warn-wash": "var(--warn-wash)",
        "warn-edge": "var(--warn-edge)",
        "ok-wash": "var(--ok-wash)",
        "ok-edge": "var(--ok-edge)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        indic: ["var(--font-indic)"],
        mono: ["var(--font-mono)"],
      },
      // 8-pt grid: Tailwind's 4-pt scale already contains every multiple of 8;
      // these are the two extra stops the two-pane layout needs.
      spacing: { 18: "4.5rem", 108: "27rem" },
    },
  },
  plugins: [],
};
