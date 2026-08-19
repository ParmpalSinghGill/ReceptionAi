# ReceptionAi — Design System

Extracted from the ReceptionAi marketing page. This file is the single source
of truth for both `apps/marketing` and `apps/web` (admin). If a component in
either app hardcodes a hex value, it is wrong.

---

## 1. Palette

Dark-first. The design is a deep desaturated teal-green, not a neutral grey with
an accent — the surfaces themselves carry the hue, which is what makes it read
as one system rather than "dark mode with green buttons".

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0B1F1C` | Page background, deepest layer |
| `--bg-raised` | `#102A26` | Cards, panels, the call transcript box |
| `--bg-header` | `#0E2521` | Nav bar; sits between bg and raised |
| `--bg-inset` | `#0A1A17` | Wells, code, inset rows |
| `--border` | `#1C3B35` | Hairlines, card edges |
| `--border-strong` | `#2A544B` | Focus rings, active edges |
| `--fg` | `#E8F2EF` | Primary text |
| `--fg-muted` | `#9FB8B2` | Body copy, secondary |
| `--fg-subtle` | `#6E8B85` | Labels, captions, `CALLER`/`DESK` tags |
| `--accent` | `#3DDCA6` | Primary action, the "Ai" in the wordmark, stat numerals |
| `--accent-hover` | `#5CE8B8` | Hover state |
| `--accent-press` | `#2BC08C` | Active/pressed |
| `--accent-fg` | `#062B21` | Text ON accent (dark on bright — never white) |
| `--accent-dim` | `#1E4D42` | Accent at low emphasis, filled chips |
| `--live` | `#3DDCA6` | The pulsing "Live" / "Incoming call" dot |

Status colors — desaturated to sit inside the teal world rather than fight it:

| Token | Value | Use |
|---|---|---|
| `--ok` | `#3DDCA6` | Confirmed, booked, healthy |
| `--warn` | `#E8C468` | Needs attention, degraded |
| `--danger` | `#E87D7D` | Failed, cancelled, over quota |
| `--info` | `#6FB3D9` | Neutral notice |

**Light theme** exists (some admin users demand it) and inverts surfaces while
keeping the same accent. Every component must be written with both, per the
artifact/theme rule: `@media (prefers-color-scheme)` plus a `[data-theme]`
override that wins in both directions.

---

## 2. Type

Two families, and the contrast between them is the whole personality:

- **Display** — a high-contrast serif. The screenshot's "The front desk that
  *always* picks up." is serif with a true italic on the emphasis word. Use
  `Instrument Serif`, `Fraunces`, or `Newsreader`. Never faux-italic.
- **UI/Body** — a geometric humanist sans: `Inter`, or `Geist`.
- **Numerals** — display serif at large sizes for stat tiles (`24/7`, `~1.9s`),
  with `font-variant-numeric: tabular-nums` anywhere numbers update live, so
  they don't jitter.

| Role | Size / Weight | Notes |
|---|---|---|
| Hero | 64–76px serif, 1.05 line-height | Emphasis word in italic accent |
| Page title | 30px serif | Admin page headers |
| Section | 20px sans 600 | |
| Body | 15–16px sans 400, 1.6 | `--fg-muted` |
| Label | 11px sans 600, `0.08em` tracking, uppercase | `--fg-subtle`; the `CALLER`/`DESK` tags |
| Stat numeral | 44px serif | `--accent` |
| Mono | `JetBrains Mono` | IDs, phone numbers, JSON |

---

## 3. Shape and depth

- Radius: `8px` inputs · `12px` cards · `999px` pills and buttons
- Buttons are **fully rounded pills** — this is distinctive to the design and
  must not drift to 6px rounded rectangles
- Borders do the work, not shadows. One hairline `--border`; shadows only on
  overlays (modal, dropdown, toast)
- The primary button carries a small filled dot before its label (`● Request a
  call`) — a signature detail, reused for the live indicator

---

## 4. Motion

- Durations: `120ms` micro (hover) · `200ms` standard · `320ms` overlays
- Easing: `cubic-bezier(0.2, 0, 0, 1)`
- The live dot pulses on a `2s` loop
- Streaming text appears by token, never by typewriter-faking a completed string
- **All of it collapses under `prefers-reduced-motion`** — the pulse becomes a
  static dot, transitions go to `0ms`. This is not optional.

---

## 5. Signature components

These carry the brand; get them right and everything else follows.

**Live badge** — pill, `--bg-raised`, hairline border, pulsing `--accent` dot,
label in `--fg-muted`. Used for "Live · answering as Hospital / Clinic" and for
the admin's active-call indicator.

**Conversation card** — `--bg-raised`, 12px radius, a header row (status dot +
label, elapsed time right-aligned), an optional waveform, then transcript rows
of `LABEL` (11px `--fg-subtle`) beside message text, and a footer result strip
in `--accent-dim` with a check icon.

**Stat tile** — big serif numeral in `--accent`, an uppercase micro-label, then
a line of `--fg-muted` explanation. Used on both marketing and dashboard.

**Waveform** — vertical bars, `3px` wide, `2px` gap, `--accent`, heights driven
by real amplitude. When idle it is a flat line of minimum-height bars, never a
fake animation implying audio that isn't there.

---

## 6. Layout

- Content max-width `1200px`; prose max-width `68ch`
- Spacing scale: `4 · 8 · 12 · 16 · 24 · 32 · 48 · 64 · 96`
- Marketing: single column, generous vertical rhythm, hero split 50/50 above
  `1024px` and stacked below
- Admin: `240px` sidebar + fluid main; sidebar collapses to icons under
  `1280px` and to a drawer under `768px`

---

## 7. Accessibility — WCAG 2.2 AA, non-negotiable

- `--fg` on `--bg` ≈ 14:1. `--fg-muted` on `--bg` ≈ 7:1. `--fg-subtle` on
  `--bg` ≈ 4.6:1 — passes for normal text, and is the floor: nothing dimmer
  than `--fg-subtle` may carry text.
- `--accent-fg` on `--accent` ≈ 9:1. **Never white on `--accent`** — it fails.
- Focus is a visible 2px `--accent` ring with a 2px offset, on every
  interactive element. Never `outline: none` without a replacement.
- Status is never color alone: a confirmed booking shows a check glyph and the
  word, not just green.
- Streaming assistant text lives in `aria-live="polite"`; a live call indicator
  announces on state change, not on every tick.
- Full keyboard operation, including the chat, the transcript viewer, and the
  dynamic manifest-driven forms.
