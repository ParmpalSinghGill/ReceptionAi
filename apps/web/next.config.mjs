import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const ui = path.join(here, "..", "..", "packages", "ui");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  eslint: { ignoreDuringBuilds: true },

  // A pnpm workspace dependency normally resolves through a symlink in
  // node_modules. This repo may sit on NTFS/exFAT, which cannot create one, so
  // these aliases point the bundler straight at the package source — equivalent
  // for a transpiled package and symlink-free. tsconfig.json carries matching
  // `paths` so the type checker and editor agree with the bundler.
  // apps/marketing/next.config.mjs does exactly the same.
  webpack: (config) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      "@receptionai/ui/tailwind-preset": path.join(ui, "tailwind-preset.ts"),
      "@receptionai/ui/lib": path.join(ui, "src/lib"),
      "@receptionai/ui/styles": path.join(ui, "src/styles"),
      "@receptionai/ui": path.join(ui, "src/index.ts"),
    };
    return config;
  },
  async rewrites() {
    // 06 §8.1's load-bearing deployment assumption: apps/web and apps/api are
    // served from the SAME ORIGIN, with /api/v1/* reverse-proxied to FastAPI.
    // If they were on different origins the refresh cookie would be
    // third-party, and third-party cookies are gone. In development that
    // proxy is this rewrite.
    //
    // Phase 1's API is narrow (PHASE1-API-CONTRACT.md), so when API_ORIGIN is
    // unset the app runs against in-process mock handlers instead and this
    // rewrite is not installed.
    //
    // `API_DATA_ORIGIN` installs the proxy WITHOUT switching the shell out of
    // demo mode, which is what lets a screen be developed against real rows
    // while sign-in is still unwired. `API_ORIGIN` remains the production
    // switch and implies the data plane; the split exists so turning on real
    // data for one screen does not lock the whole portal behind a login that
    // Phase 1 has no BFF route for.
    const origin = process.env.API_ORIGIN ?? process.env.API_DATA_ORIGIN;
    if (!origin) return [];
    return [{ source: "/api/v1/:path*", destination: `${origin}/api/v1/:path*` }];
  },
};

export default nextConfig;
