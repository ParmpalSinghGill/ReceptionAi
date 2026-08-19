"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef } from "react";

import { Wordmark } from "@/components/shell/Wordmark";
import { NAV_SECTIONS, type NavItem } from "@/lib/nav";
import { usePermissions } from "@/lib/auth/permissions";
import { cn } from "@/lib/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/**
 * The nav list, shared by all three presentations (full sidebar, icon rail,
 * drawer). `collapsed` is a prop rather than a media query inside the item so
 * the drawer can render the FULL labels at any viewport — a drawer that
 * showed icons only would be a rail in a sheet.
 */
function NavList({
  collapsed,
  liveCount,
  onNavigate,
}: {
  collapsed: boolean;
  liveCount: number;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();
  const { can } = usePermissions();

  const renderItem = (item: NavItem) => {
    if (item.perm && !can(item.perm)) return null;
    const active = isActive(pathname, item.href);
    const Icon = item.icon;
    const badge = item.badgeKey === "live" && liveCount > 0 ? liveCount : null;

    const link = (
      <Link
        href={item.href}
        onClick={onNavigate}
        aria-current={active ? "page" : undefined}
        className={cn(
          "group relative flex items-center gap-3 rounded-input px-3 py-2 text-sm transition-colors duration-micro ease-brand",
          collapsed && "justify-center px-0",
          active
            ? "bg-accent-dim text-fg"
            : "text-fg-muted hover:bg-bg-inset hover:text-fg",
        )}
      >
        {/* The active marker is a shape, not only a tint — `aria-current`
            carries it for assistive tech and this bar carries it visually. */}
        {active && (
          <span
            aria-hidden="true"
            className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-pill bg-accent"
          />
        )}
        <Icon className={cn("size-4 shrink-0", active && "text-accent")} aria-hidden="true" />
        {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
        {badge !== null &&
          (collapsed ? (
            <span
              aria-hidden="true"
              className="absolute right-2 top-1.5 size-2 rounded-pill bg-live"
            />
          ) : (
            <span className="rounded-pill bg-accent-dim px-1.5 py-0.5 text-xs tabular text-accent">
              {badge}
            </span>
          ))}
        {collapsed && <span className="sr-only">{item.label}</span>}
      </Link>
    );

    // In the rail the label is gone, so it must come back as a tooltip —
    // otherwise the rail is a column of unlabelled glyphs.
    return (
      <li key={item.key}>
        {collapsed ? (
          <Tooltip>
            <TooltipTrigger asChild>{link}</TooltipTrigger>
            <TooltipContent side="right">
              {item.label}
              {badge !== null && ` — ${badge} live`}
            </TooltipContent>
          </Tooltip>
        ) : (
          link
        )}
      </li>
    );
  };

  return (
    <TooltipProvider delayDuration={200}>
      <nav aria-label="Main" className="flex flex-col gap-6 px-3">
        {NAV_SECTIONS.map((section) => {
          const items = section.items.map(renderItem).filter(Boolean);
          if (items.length === 0) return null;
          return (
            <div key={section.key}>
              {/* The group heading is hidden in the rail but kept for screen
                  readers, so the grouping survives the collapse. */}
              <h2
                className={cn(
                  "mb-2 px-3 text-label uppercase text-fg-subtle",
                  collapsed && "sr-only",
                )}
              >
                {section.label}
              </h2>
              <ul className="flex flex-col gap-0.5">{items}</ul>
            </div>
          );
        })}
      </nav>
    </TooltipProvider>
  );
}

/**
 * DESIGN.md §6: 240px sidebar + fluid main; collapses to ICONS under 1280px
 * and to a DRAWER under 768px.
 *
 * The three modes are driven by CSS (`hidden md:flex rail:w-60`) rather than
 * by a JS resize listener, so there is no flash of the wrong layout on first
 * paint and no re-render on every resize tick. Only the drawer's open/closed
 * state is JS, because it is genuinely stateful.
 */
export function Sidebar({ liveCount }: { liveCount: number }) {
  return (
    <aside
      className={cn(
        // Hidden below md (the drawer takes over), 72px rail from md, full
        // 240px from `rail` (1280px).
        "hidden md:flex md:w-[72px] rail:w-60 shrink-0 flex-col border-r border-line bg-bg-header",
        "sticky top-0 h-dvh overflow-y-auto py-4",
      )}
    >
      <div className="mb-6 flex items-center justify-center rail:justify-start rail:px-6">
        <Wordmark collapsed />
        <span className="hidden rail:inline">
          <Wordmark />
        </span>
      </div>

      {/* Two renders, one visible at a time. Rendering the rail and the full
          list separately keeps `collapsed` a static prop per instance, which
          is what lets the tooltip exist only where the label is missing. */}
      <div className="rail:hidden">
        <NavList collapsed liveCount={liveCount} />
      </div>
      <div className="hidden rail:block">
        <NavList collapsed={false} liveCount={liveCount} />
      </div>
    </aside>
  );
}

/**
 * The under-768px drawer.
 *
 * Deliberately not a Radix Dialog: this is navigation, and Radix's Dialog
 * would make the rest of the page inert while it is open, which is right for
 * a modal and wrong for a nav sheet a user may want to dismiss by clicking the
 * content behind it. It does still need the two things a Dialog gives for
 * free, so both are implemented here: Escape closes it, and focus returns to
 * the trigger.
 */
export function NavDrawer({
  open,
  onClose,
  liveCount,
  triggerRef,
}: {
  open: boolean;
  onClose: () => void;
  liveCount: number;
  triggerRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // Move focus into the panel so a keyboard user's next Tab is inside the
    // nav rather than back at the top of the page.
    panelRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const close = () => {
    onClose();
    // Restore focus to the button that opened it — otherwise focus falls to
    // <body> and a screen reader loses its place entirely.
    triggerRef.current?.focus();
  };

  if (!open) return null;

  return (
    <div className="md:hidden">
      <div
        className="fixed inset-0 z-40 bg-black/60 animate-overlay-in"
        onClick={close}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="false"
        aria-label="Navigation"
        className="fixed left-0 top-0 z-50 h-dvh w-[min(17rem,85vw)] overflow-y-auto border-r border-line bg-bg-header py-4 shadow-overlay animate-drawer-in focus:outline-none"
      >
        <div className="mb-6 px-6">
          <Wordmark />
        </div>
        <NavList collapsed={false} liveCount={liveCount} onNavigate={close} />
      </div>
    </div>
  );
}
