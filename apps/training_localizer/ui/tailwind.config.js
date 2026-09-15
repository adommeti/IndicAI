/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // The token layer in src/styles/tokens.css is the source of truth; Tailwind
      // reads it so a colour can never be defined in two places.
      colors: {
        bg: "var(--bg)",
        surface: "var(--surface)",
        "surface-2": "var(--surface-2)",
        border: "var(--border)",
        ink: "var(--ink)",
        muted: "var(--ink-muted)",
        accent: "var(--accent)",
        danger: "var(--danger)",
        warn: "var(--warn)",
        ok: "var(--ok)",
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        indic: ["var(--font-indic)"],
        mono: ["var(--font-mono)"],
      },
      spacing: { 18: "4.5rem" },
    },
  },
  plugins: [],
};
