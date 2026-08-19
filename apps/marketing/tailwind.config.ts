import type { Config } from "tailwindcss";
// Relative import: the tailwind config is loaded by PostCSS/jiti, outside the
// webpack alias that substitutes for the unavailable workspace symlink.
import preset from "../../packages/ui/tailwind-preset";

export default {
  presets: [preset],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    // The shared package's classes must be scanned too, or the utilities its
    // components reference get tree-shaken out of the marketing bundle.
    "../../packages/ui/src/**/*.{ts,tsx}",
  ],
} satisfies Config;
