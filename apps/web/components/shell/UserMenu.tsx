"use client";

import { useRouter } from "next/navigation";
import { LogOut, ShieldCheck, User } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSession, useTenant } from "@/components/shell/Providers";
import { setAccessToken } from "@/lib/auth/token-store";
import { toast } from "@/components/ui/toast";

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((p) => p[0]?.toUpperCase() ?? "")
    .join("");
}

export function UserMenu() {
  const { displayName, email } = useSession();
  const { roles, aal } = useTenant();
  const router = useRouter();

  const signOut = async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
    } catch {
      /* the local token is dropped either way; a failed call must not strand
         the user in a session they asked to leave */
    }
    setAccessToken(null);
    router.push("/login");
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex size-9 items-center justify-center rounded-pill border border-line bg-bg-raised text-xs font-semibold text-fg transition-colors duration-micro hover:border-strong"
        aria-label={`Account menu for ${displayName}`}
      >
        <span aria-hidden="true">{initials(displayName)}</span>
      </DropdownMenuTrigger>

      <DropdownMenuContent align="end" className="min-w-64">
        <DropdownMenuLabel>Signed in</DropdownMenuLabel>
        <div className="px-3 pb-2">
          <p className="truncate text-sm font-medium text-fg">{displayName}</p>
          <p className="truncate text-xs text-fg-subtle">{email}</p>
          <p className="mt-1 text-xs text-fg-subtle">
            {roles.join(", ")}
            {/* aal is surfaced rather than hidden: a user who cannot find the
                billing screen deserves to know it is a re-auth away, not a
                permission they lack. */}
            {aal < 2 && " · not re-verified"}
          </p>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => router.push("/settings")}>
          <User aria-hidden="true" />
          Profile and business details
        </DropdownMenuItem>
        {aal < 2 && (
          <DropdownMenuItem
            onSelect={() =>
              toast({
                title: "Identity confirmation",
                description:
                  "Sensitive actions such as billing and API keys ask you to re-enter your password. This is a demo build, so the challenge is not wired up.",
              })
            }
          >
            <ShieldCheck aria-hidden="true" />
            Confirm my identity
          </DropdownMenuItem>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={signOut}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
