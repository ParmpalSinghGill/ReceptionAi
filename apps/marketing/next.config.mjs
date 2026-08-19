import path from "node:path";
import { fileURLToPath } from "node:url";

const dir = path.dirname(fileURLToPath(import.meta.url));
const ui = path.resolve(dir, "../../packages/ui");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // The UI package ships TypeScript/JSX source rather than a build artifact,
  // so Next must compile it as if it were app code.
  transpilePackages: ["@receptionai/ui"],

  // This repository lives on an NTFS volume (ntfs3), which does not support
  // symlinks — so pnpm cannot create the usual
  // `node_modules/@receptionai/ui -> packages/ui` link that a workspace
  // dependency normally resolves through. These aliases point the bundler at
  // the package source directly, which is equivalent for a transpiled package
  // and needs no symlink. `tsconfig.json` carries matching `paths` so the type
  // checker and the editor agree with the bundler.
  webpack: (config) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      "@receptionai/ui/tailwind-preset": path.join(ui, "tailwind-preset.ts"),
      "@receptionai/ui/styles": path.join(ui, "src/styles"),
      "@receptionai/ui": path.join(ui, "src/index.ts"),
    };
    return config;
  },
};

export default nextConfig;
