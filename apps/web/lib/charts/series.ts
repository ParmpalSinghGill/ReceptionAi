const SLOTS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
  "var(--chart-7)",
  "var(--chart-8)",
] as const;

/**
 * Stable colour assignment keyed on the SERIES KEY, not its index.
 *
 * The bug this prevents: a user filters out "web_chat", every remaining series
 * shifts up an index, and "voice_inbound" changes from blue to teal. The data
 * did not change; the picture did. Users read that as a data change and lose
 * trust in every chart in the product.
 *
 * The order is registration order within a chart's declared key list, which is
 * itself stable (it comes from the endpoint's series order, not from sorting
 * by value). A series that appears in one time range and not another keeps its
 * colour across both.
 */
export function makeColorScale(allKeys: readonly string[]) {
  const assignment = new Map<string, string>();
  allKeys.forEach((key, i) => {
    if (i < SLOTS.length) assignment.set(key, SLOTS[i]!);
  });
  // Past eight, a series gets the axis grey rather than a cycled hue: two
  // series sharing a colour is a lie, and a generated ninth hue is unvalidated.
  return (key: string) => assignment.get(key) ?? "var(--chart-axis)";
}

/**
 * The all-pairs safe subset, for chart forms where any two marks can sit next
 * to each other (scatter, bubble, small multiples).
 *
 * The categorical palette is validated on ADJACENT pairs, which is the right
 * test for lines and stacked bars where only neighbours touch. Under
 * `--pairs all` the light palette's slot 3 collapses against slot 1
 * (ΔE 4.1, protanopia), so those forms must not use the first three slots in
 * order. Slots 1, 2 and 4 clear all-pairs in BOTH modes and are the cap.
 */
export const ALL_PAIRS_SAFE_SLOTS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-4)",
] as const;

export function makeScatterColorScale(allKeys: readonly string[]) {
  const assignment = new Map<string, string>();
  allKeys.slice(0, ALL_PAIRS_SAFE_SLOTS.length).forEach((key, i) => {
    assignment.set(key, ALL_PAIRS_SAFE_SLOTS[i]!);
  });
  return (key: string) => assignment.get(key) ?? "var(--chart-axis)";
}

/** The sequential ramp, for magnitude (heatmaps). One hue, ordered. */
export const SEQUENTIAL_RAMP = [
  "var(--chart-seq-1)",
  "var(--chart-seq-2)",
  "var(--chart-seq-3)",
  "var(--chart-seq-4)",
  "var(--chart-seq-5)",
] as const;

export function sequentialStep(value: number, max: number): string {
  if (max <= 0) return SEQUENTIAL_RAMP[0]!;
  const idx = Math.min(
    SEQUENTIAL_RAMP.length - 1,
    Math.floor((value / max) * SEQUENTIAL_RAMP.length),
  );
  return SEQUENTIAL_RAMP[idx]!;
}
