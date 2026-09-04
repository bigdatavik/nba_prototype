import type { Config } from "tailwindcss";

// Design tokens from docs/NBA_Demo_Deck (REACT_REBUILD_PLAN §6). One visual
// language: deep-ink dark shell, teal = analytics/Genie, coral = NBA action.
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0B111C",
        panel: "#131C2B",
        "panel-2": "#1A2536",
        line: "#26344A",
        text: "#E7EEF7",
        muted: "#94A5BD",
        teal: "#2DD4BF",
        coral: "#FF7A59",
        good: "#3FD08A",
        warn: "#FBBF24",
        fail: "#F87171",
      },
      fontFamily: {
        display: ['"Bricolage Grotesque"', "system-ui", "sans-serif"],
        sans: ['"IBM Plex Sans"', "system-ui", "sans-serif"],
        mono: ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      fontVariantNumeric: {
        tabular: "tabular-nums",
      },
      boxShadow: {
        panel: "0 1px 0 rgba(255,255,255,0.02), 0 8px 30px rgba(0,0,0,0.35)",
        drawer: "-24px 0 60px rgba(0,0,0,0.5)",
      },
      keyframes: {
        "slide-in": {
          from: { transform: "translateX(100%)" },
          to: { transform: "translateX(0)" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "slide-in": "slide-in 0.28s cubic-bezier(0.22,1,0.36,1)",
        "fade-in": "fade-in 0.2s ease-out",
      },
    },
  },
  plugins: [],
} satisfies Config;
