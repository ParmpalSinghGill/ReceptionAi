"use client";

import { Menu } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { NavDrawer, Sidebar } from "@/components/shell/Sidebar";
import { TenantSwitcher } from "@/components/shell/TenantSwitcher";
import { ThemeToggle } from "@/components/shell/ThemeToggle";
import { UserMenu } from "@/components/shell/UserMenu";
import { EnvironmentBadge } from "@/components/shell/EnvironmentBadge";
import { useTenant } from "@/components/shell/Providers";
import { useLiveConversations } from "@/lib/query/hooks";
import { toast } from "@/components/ui/toast";

export function AppShell({ children }: { children: React.ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerButtonRef = useRef<HTMLButtonElement>(null);
  const pathname = usePathname();
  const router = useRouter();
  const { environment } = useTenant();
  const live = useLiveConversations();

  // Close the drawer on navigation — otherwise it stays open over the page
  // the user just chose.
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname]);

  /**
   * The listeners for the two events lib/api/client.ts dispatches. The fetch
   * wrapper cannot navigate (it has no router), so it announces and this
   * component — which does have one — decides what that means.
   */
  useEffect(() => {
    const onExpired = () => {
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      router.push(`/login?next=${next}`);
    };
    const onStepUp = () => {
      toast({
        title: "Confirm your identity to continue",
        description: "This action needs a recent sign-in. In this demo build the challenge is not wired up.",
        variant: "warning",
      });
    };
    window.addEventListener("auth:expired", onExpired);
    window.addEventListener("auth:step-up", onStepUp as EventListener);
    return () => {
      window.removeEventListener("auth:expired", onExpired);
      window.removeEventListener("auth:step-up", onStepUp as EventListener);
    };
  }, [router]);

  const liveCount = live.data?.length ?? 0;

  return (
    <div className="flex min-h-dvh">
      {/* 2.4.1 Bypass Blocks: the first focusable element in the shell,
          visually hidden until focused. */}
      <a
        href="#main"
        className="sr-only-focusable absolute left-4 top-4 z-[100] rounded-pill bg-accent px-4 py-2 text-sm font-medium text-accent-fg"
      >
        Skip to main content
      </a>

      <Sidebar liveCount={liveCount} />
      <NavDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        liveCount={liveCount}
        triggerRef={drawerButtonRef}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-3 border-b border-line bg-bg-header px-4 sm:px-6">
          <button
            ref={drawerButtonRef}
            type="button"
            onClick={() => setDrawerOpen(true)}
            aria-expanded={drawerOpen}
            aria-label="Open navigation"
            className="flex size-9 shrink-0 items-center justify-center rounded-pill text-fg-muted transition-colors duration-micro hover:bg-bg-inset hover:text-fg md:hidden"
          >
            <Menu className="size-5" aria-hidden="true" />
          </button>

          <TenantSwitcher />

          {/* On a narrow viewport the badge inside the switcher trigger is cut
              off, so the environment is repeated here where there is room. */}
          <span className="hidden sm:block">
            <EnvironmentBadge environment={environment} />
          </span>

          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <UserMenu />
          </div>
        </header>

        {/* `min-w-0` so a wide child (a table, a chart, the live-call strip)
            scrolls inside its own container rather than stretching the shell
            and making the page body scroll sideways (1.4.10 Reflow). */}
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 focus:outline-none">
          {children}
        </main>
      </div>
    </div>
  );
}
