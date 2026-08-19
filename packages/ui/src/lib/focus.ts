/**
 * The one focus treatment, shared by every interactive component.
 *
 * DESIGN.md §7: a visible 2px --accent ring with a 2px offset on every
 * interactive element, and never `outline: none` without a replacement.
 * `focus-visible` rather than `focus` so a mouse click on a button does not
 * paint a ring, while keyboard and programmatic focus always do.
 *
 * Kept as a constant instead of copy-pasted per component so the ring cannot
 * drift between Button and Input.
 */
export const focusRing =
  "outline-none focus-visible:ring-2 focus-visible:ring-accent " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--ring-offset)]";

/**
 * For elements whose own background is not --bg (rows inside a raised card,
 * for instance), the offset colour must match what sits behind the ring or
 * the offset reads as a halo of the wrong colour.
 */
export const focusRingRaised =
  "outline-none focus-visible:ring-2 focus-visible:ring-accent " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-raised)]";
