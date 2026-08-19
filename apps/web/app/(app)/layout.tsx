import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { AppShell } from "@/components/shell/AppShell";
import { Providers } from "@/components/shell/Providers";
import { getServerSession } from "@/lib/auth/server";

/**
 * THE AUTHENTICATED SHELL — a Server Component.
 *
 * Layer 2 of 06 §8.3's three-layer model. The middleware (layer 1) redirected
 * anyone with no refresh cookie before the app ran; this layer actually
 * exchanges the cookie and gets a validated principal, so a forged or expired
 * cookie fails here. Layer 3 — the API authorizing every request
 * independently — is the only layer that matters for security; these two
 * exist so an unauthorized user sees a login page rather than a portal full
 * of empty panels and 403 toasts.
 *
 * Because this layout asserts the session, every component beneath it can
 * rely on `useTenant()` without a guard (§7.5).
 */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const session = await getServerSession(await cookies());

  if (!session) redirect("/login");
  if (!session.onboarding_complete) redirect("/setup");

  return (
    <Providers session={session}>
      <AppShell>{children}</AppShell>
    </Providers>
  );
}
