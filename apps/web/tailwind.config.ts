import type { Config } from "tailwindcss";

// Relative, not the "@receptionai/ui/..." package name: resolving by name needs
// a workspace symlink in node_modules, and this repo may live on a filesystem
// that cannot create symlinks (NTFS/exFAT). apps/marketing does the same.
import preset from "../../packages/ui/tailwind-preset";

/**
 * The admin portal's Tailwind config is the shared preset plus a content
 * glob. Every colour, radius, duration and type step comes from
 * packages/ui — there is no palette here, by design.
 */
const config: Config = {
  presets: [preset],
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
    // The shared components live in packages/ui and carry their own classes;
    // without this glob Tailwind would purge the utilities they rely on.
    "../../packages/ui/src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      screens: {
        // DESIGN.md §6's admin breakpoints, named so the shell reads
        // declaratively: the sidebar collapses to icons under `rail` and
        // becomes a drawer under `md`.
        rail: "1280px",
      },
      gridTemplateColumns: {
        "shell-full": "240px minmax(0, 1fr)",
        "shell-rail": "72px minmax(0, 1fr)",
      },
    },
  },
};

export default config;
