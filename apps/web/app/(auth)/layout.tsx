import { Wordmark } from "@/components/shell/Wordmark";

/**
 * The unauthenticated group has NO principal (06 §7.5), so it must not fetch
 * a session — doing so would be a guaranteed 401 on every login page render.
 * No shell, no nav, no tenant context.
 */
export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-8 px-4 py-12">
      <Wordmark />
      <main className="w-full max-w-md">{children}</main>
      <p className="max-w-prose text-center text-xs text-fg-subtle">
        ReceptionAi answers your phone, books appointments and never puts anyone on hold.
      </p>
    </div>
  );
}
