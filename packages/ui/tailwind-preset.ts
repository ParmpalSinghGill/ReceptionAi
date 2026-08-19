import type { Config } from "tailwindcss";

/**
 * Tailwind preset for the ReceptionAi design system.
 *
 * Every value here resolves to a CSS custom property from `tokens.css`, which
 * is what makes theme switching work at all: a utility like `bg-raised`
 * compiles to `background-color: var(--bg-raised)`, so flipping `data-theme`
 * on an ancestor re-themes the element with no class changes and no JS.
 *
 * The corollary, and the rule DESIGN.md states outright: a component that
 * writes a hex is wrong. There is no hex in this file either — only var().
 */
const preset: Config = {
  content: [],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "var(--bg)",
          raised: "var(--bg-raised)",
          header: "var(--bg-header)",
          inset: "var(--bg-inset)",
        },
        border: {
          DEFAULT: "var(--border)",
          strong: "var(--border-strong)",
        },
        /* `line` is an alias for the hairline border colour, usable as a
           text/bg/border colour without colliding with Tailwind's built-in
           `border` utility namespace. */
        line: {
          DEFAULT: "var(--border)",
          strong: "var(--border-strong)",
        },
        fg: {
          DEFAULT: "var(--fg)",
          muted: "var(--fg-muted)",
          subtle: "var(--fg-subtle)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          hover: "var(--accent-hover)",
          press: "var(--accent-press)",
          fg: "var(--accent-fg)",
          dim: "var(--accent-dim)",
        },
        live: "var(--live)",
        /* Scrim behind modal overlays. */
        scrim: "var(--scrim)",
        ok: {
          DEFAULT: "var(--ok)",
          fg: "var(--ok-fg)",
          dim: "var(--ok-dim)",
          /* `on` = text on the SOLID fill; `fg` = the hue as readable text on
             --bg/--bg-raised. Conflating them makes a filled button invisible
             in dark mode. */
          on: "var(--ok-on)",
        },
        warn: {
          DEFAULT: "var(--warn)",
          fg: "var(--warn-fg)",
          dim: "var(--warn-dim)",
          /* `on` = text on the SOLID fill; `fg` = the hue as readable text on
             --bg/--bg-raised. Conflating them makes a filled button invisible
             in dark mode. */
          on: "var(--warn-on)",
        },
        danger: {
          DEFAULT: "var(--danger)",
          fg: "var(--danger-fg)",
          dim: "var(--danger-dim)",
          /* `on` = text on the SOLID fill; `fg` = the hue as readable text on
             --bg/--bg-raised. Conflating them makes a filled button invisible
             in dark mode. */
          on: "var(--danger-on)",
        },
        info: {
          DEFAULT: "var(--info)",
          fg: "var(--info-fg)",
          dim: "var(--info-dim)",
          /* `on` = text on the SOLID fill; `fg` = the hue as readable text on
             --bg/--bg-raised. Conflating them makes a filled button invisible
             in dark mode. */
          on: "var(--info-on)",
        },

        /* Categorical chart slots (06 §12.1). Fixed order, never cycled —
           `lib/charts/series.ts` assigns them by series KEY, not by index. */
        chart: {
          1: "var(--chart-1)",
          2: "var(--chart-2)",
          3: "var(--chart-3)",
          4: "var(--chart-4)",
          5: "var(--chart-5)",
          6: "var(--chart-6)",
          7: "var(--chart-7)",
          8: "var(--chart-8)",
          grid: "var(--chart-grid)",
          axis: "var(--chart-axis)",
          surface: "var(--chart-surface)",
        },
      },

      ringColor: { DEFAULT: "var(--ring)" },
      ringOffsetColor: { DEFAULT: "var(--ring-offset)" },
      borderColor: { DEFAULT: "var(--border)" },

      fontFamily: {
        display: "var(--font-display)",
        sans: "var(--font-sans)",
        mono: "var(--font-mono)",
      },

      fontSize: {
        hero: ["var(--text-hero)", { lineHeight: "var(--text-hero-lh)" }],
        "page-title": ["var(--text-page-title)", { lineHeight: "1.2" }],
        section: ["var(--text-section)", { lineHeight: "1.35" }],
        body: ["var(--text-body)", { lineHeight: "var(--text-body-lh)" }],
        "body-sm": ["var(--text-body-sm)", { lineHeight: "var(--text-body-lh)" }],
        label: [
          "var(--text-label)",
          { lineHeight: "1.2", letterSpacing: "var(--text-label-tracking)" },
        ],
        stat: ["var(--text-stat)", { lineHeight: "1.1" }],
      },

      borderRadius: {
        input: "var(--radius-input)",
        card: "var(--radius-card)",
        pill: "var(--radius-pill)",
      },

      /**
       * The DESIGN.md §6 scale (4·8·12·16·24·32·48·64·96), namespaced as
       * `step-*` — so `p-step5` is 24px, `py-step9` is 96px.
       *
       * These deliberately do NOT override Tailwind's numeric keys. Steps 1-4
       * happen to match Tailwind's defaults, but 5-9 do not (design 5 is 24px,
       * Tailwind's is 20px), and overriding the numeric scale silently
       * redefines EVERY utility that reads spacing — `h-8` became 64px instead
       * of 32px, `size-8` likewise, which broke every icon and dot in the
       * system. Keeping the brand scale in its own namespace means the two
       * cannot collide.
       */
      spacing: {
        step1: "var(--space-1)",
        step2: "var(--space-2)",
        step3: "var(--space-3)",
        step4: "var(--space-4)",
        step5: "var(--space-5)",
        step6: "var(--space-6)",
        step7: "var(--space-7)",
        step8: "var(--space-8)",
        step9: "var(--space-9)",
      },

      maxWidth: {
        content: "var(--width-content)",
        prose: "var(--width-prose)",
      },

      boxShadow: {
        overlay: "var(--shadow-overlay)",
      },

      transitionTimingFunction: {
        DEFAULT: "var(--ease)",
        brand: "var(--ease)",
      },

      transitionDuration: {
        micro: "var(--duration-micro)",
        standard: "var(--duration-standard)",
        overlay: "var(--duration-overlay)",
      },

      keyframes: {
        "live-pulse": {
          // The ring expands and fades; the dot core stays solid so the
          // indicator never fully disappears mid-cycle.
          "0%": { transform: "scale(1)", opacity: "0.55" },
          "70%": { transform: "scale(2.4)", opacity: "0" },
          "100%": { transform: "scale(2.4)", opacity: "0" },
        },
        "dot-breathe": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.5" },
        },
        "skeleton-sheen": {
          "0%": { backgroundPosition: "200% 0" },
          "100%": { backgroundPosition: "-200% 0" },
        },
        "overlay-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "dialog-in": {
          from: { opacity: "0", transform: "translateY(8px) scale(0.98)" },
          to: { opacity: "1", transform: "translateY(0) scale(1)" },
        },
        "toast-in": {
          from: { opacity: "0", transform: "translateY(12px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "drawer-in": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" },
        },
        "caret-blink": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0" },
        },
      },

      animation: {
        "live-pulse": "live-pulse var(--pulse-duration) var(--ease) infinite",
        "dot-breathe": "dot-breathe var(--pulse-duration) var(--ease) infinite",
        "skeleton-sheen": "skeleton-sheen 1.6s linear infinite",
        "overlay-in": "overlay-in var(--duration-overlay) var(--ease)",
        "dialog-in": "dialog-in var(--duration-overlay) var(--ease)",
        "toast-in": "toast-in var(--duration-standard) var(--ease)",
        "drawer-in": "drawer-in var(--duration-overlay) var(--ease)",
        "caret-blink": "caret-blink 1s step-end infinite",
      },
    },
  },
};

export default preset;
