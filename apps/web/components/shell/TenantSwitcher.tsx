"use client";

import { Building2, Check, ChevronsUpDown } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTenant } from "@/components/shell/Providers";
import { EnvironmentBadge } from "@/components/shell/EnvironmentBadge";

export function TenantSwitcher() {
  const { tenantName, tenantId, availableTenants, switchTenant, verticalKey } = useTenant();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex min-w-0 items-center gap-2 rounded-pill border border-line bg-bg-raised px-3 py-1.5 text-sm text-fg transition-colors duration-micro hover:border-strong"
        aria-label={`Current workspace: ${tenantName}. Switch workspace`}
      >
        <Building2 className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
        <span className="truncate max-w-[9rem] sm:max-w-[14rem]">{tenantName}</span>
        <ChevronsUpDown className="size-3.5 shrink-0 text-fg-subtle" aria-hidden="true" />
      </DropdownMenuTrigger>

      <DropdownMenuContent align="start" className="min-w-72">
        <DropdownMenuLabel>Workspaces</DropdownMenuLabel>
        {availableTenants.map((t) => (
          <DropdownMenuItem
            key={t.id}
            onSelect={() => {
              // Switching constructs a NEW QueryClient in Providers — the old
              // cache becomes unreachable rather than being cleared, so an
              // in-flight request from this tenant cannot resolve into the
              // next one's cache (06 §11.2).
              if (t.id !== tenantId) switchTenant(t.id);
            }}
          >
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="flex items-center gap-2">
                <span className="truncate">{t.name}</span>
                <EnvironmentBadge environment={t.environment} />
              </span>
              <span className="truncate text-xs text-fg-subtle">
                {t.vertical_key} · {t.manifest_version}
              </span>
            </span>
            {t.id === tenantId && (
              <Check className="size-4 shrink-0 text-accent" aria-label="(current)" />
            )}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <p className="px-3 py-2 text-xs text-fg-subtle">
          Signed in to the {verticalKey} vertical. Switching clears everything loaded for this
          workspace.
        </p>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
