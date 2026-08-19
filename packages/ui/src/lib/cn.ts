import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * tailwind-merge resolves conflicts by classifying each utility into a group
 * and keeping the last one per group. It ships with Tailwind's DEFAULT scales,
 * so it has no idea that `text-hero` and `text-stat` are font sizes — it sees
 * an unknown `text-*` and files it under text-COLOR. The result:
 *
 *     twMerge("font-display text-stat text-accent")  →  "font-display text-accent"
 *
 * ...silently deleting the font size from every element that sets a size and a
 * colour together, which is most of them. The stat numerals rendered at body
 * size because of exactly this.
 *
 * Declaring the custom scales here puts each utility in the right group, so a
 * size and a colour coexist and two sizes still collapse correctly.
 */
export const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      // Font sizes from the preset's `fontSize` key.
      "font-size": [
        {
          text: [
            "hero",
            "page-title",
            "section",
            "body",
            "body-sm",
            "label",
            "stat",
          ],
        },
      ],
      // Radii from the preset's `borderRadius` key, so `rounded-pill` is not
      // mistaken for an arbitrary value and dropped against `rounded-card`.
      // The group is `rounded` — tailwind-merge's own id, not "border-radius".
      rounded: [{ rounded: ["input", "card", "pill"] }],
    },
  },
});

/**
 * Merge class names, with later Tailwind utilities beating earlier ones in the
 * same group. Lets a caller override a component's defaults by passing
 * `className` without fighting source order.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
