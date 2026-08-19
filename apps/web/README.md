# apps/web — ReceptionAi admin portal

Next.js App Router + TypeScript + Tailwind + React Query. Consumes
`@receptionai/ui` for every design token; there is no colour, radius, duration
or type step defined in this app.

## Running it

```bash
pnpm install
pnpm --filter @receptionai/web dev     # http://localhost:3001
```

With no `API_ORIGIN` set the portal runs in **demo mode** against a seeded
Northgate Dental workspace, because Phase 1's API is deliberately narrow
(`docs/architecture/PHASE1-API-CONTRACT.md` lists bookings, knowledge, voice
and billing as non-goals) and a portal that cannot be opened cannot be
reviewed. Set `API_ORIGIN` to point at `apps/api` and the login page, the
server-side session exchange and the `/api/v1` proxy all activate.

`apps/web` and `apps/api` MUST be served from the same origin in every
environment. 06 §8.1 makes this load-bearing: the refresh cookie is
path-scoped to `/api/v1/auth`, and on a different origin it would be a
third-party cookie.

## ⚠️ This repository is on an NTFS volume

`/media/parmpal/Data` is mounted `ntfs3` with `windows_names`, which **does not
support symlinks**. pnpm's linker is built on them, so `pnpm install` inside
the repo does not merely fail — it exhausts the Node heap (reproduced: OOM at
`--max-old-space-size=8192`, while the identical dependency set installs in 30
seconds on ext4).

Development therefore runs from an ext4 mirror:

```bash
# one-time
mkdir -p /tmp/receptionai-web-build && cd /tmp/receptionai-web-build
pnpm add next@15.1.7 react react-dom @tanstack/react-query recharts \
  lucide-react cmdk clsx tailwind-merge class-variance-authority \
  @radix-ui/react-{dialog,dropdown-menu,popover,tabs,tooltip,select,switch,slider,radio-group,label,slot}
pnpm add -D typescript @types/{node,react,react-dom} tailwindcss postcss autoprefixer
mkdir -p node_modules/@receptionai
ln -s /tmp/receptionai-web-build/packages/ui node_modules/@receptionai/ui

# each run: mirror source (NTFS) → build dir (ext4), then start
rsync -a --delete --exclude node_modules --exclude .next \
  <repo>/apps/web/ /tmp/receptionai-web-build/apps/web/
rsync -a --delete --exclude node_modules \
  <repo>/packages/ui/ /tmp/receptionai-web-build/packages/ui/
cd /tmp/receptionai-web-build/apps/web && ../../node_modules/.bin/next dev
```

The permanent fixes, in preference order: move the repo to an ext4 volume,
develop inside WSL2/devcontainer with the workspace on the Linux filesystem,
or remount with a driver that supports symlinks. Do not "fix" this by
committing a `node_modules`.

Note: running `next build` overwrites the dev server's `.next`, which leaves it
serving 404s for its stylesheets. Stop the dev server before building.

## Layout

```
app/
  (auth)/login          unauthenticated group — no shell, no tenant context
  (app)/                the authenticated shell; layout resolves the session
    page.tsx            dashboard
    conversations/      list → transcript viewer with tool calls and latency
    bookings/ services/ hours/
    agent/              model choice, layered prompts, test conversation
    knowledge/ channels/ analytics/ users/ audit/ settings/
    forms/              the manifest-driven renderer, three verticals
  api/auth/*            BFF: cookie ↔ token only, never a data proxy
components/
  shell/                sidebar, topbar, tenant switcher, theme toggle
  forms/                DynamicForm, FieldShell, ErrorSummary, widgets
  charts/               Recharts wrappers + the table relief
  conversations/ dashboard/ ui/
lib/
  api/                  fetch wrapper, ApiProblem (RFC 9457)
  auth/                 memory-only token store, permissions, server session
  query/                QueryClient, the hook key factory, the cache guard
  forms/                predicate evaluator, validation mirror
  charts/               colour scale, formatting
  mock/                 realistic demo data + compiled RenderDocuments
```

## Tenant isolation — the security boundary

Three independent mechanisms (06 §11.2), because one is not enough for a bug
class this severe:

1. **A new `QueryClient` per tenant** (`components/shell/Providers.tsx`).
   Switching constructs a new client rather than calling `.clear()`, which
   races: an in-flight request from tenant A can resolve after the clear and
   write into tenant B's fresh cache.
2. **Every key is tenant-prefixed by construction** (`lib/query/keys.ts`). The
   factory is a *hook*, so a key cannot be built without a tenant in scope.
3. **A dev-time throwing guard** (`lib/query/guard.ts`), installed during
   render alongside the client it watches — not in an effect, which would run
   after children have already registered queries and would leave the outgoing
   guard racing the incoming one.

## Verified

`tsc --noEmit` clean; `next build` succeeds for all 15 routes. Every route was
loaded in headless Chrome at 375 / 1280 / 1920 with zero console errors and no
horizontal page overflow, and the theme toggle, tenant switch, form
conditionals, error-summary focus and unknown-widget degradation were each
driven and asserted. See the parent report for the list.
