import {
  Activity,
  BarChart3,
  BookOpen,
  Bot,
  CalendarDays,
  CalendarRange,
  Clock,
  LayoutDashboard,
  MessagesSquare,
  Radio,
  ScrollText,
  Sparkles,
  Users,
} from "lucide-react";
import type { ComponentType } from "react";

import type { Perm } from "@/lib/auth/permissions";

export interface NavItem {
  key: string;
  label: string;
  href: string;
  icon: ComponentType<{ className?: string }>;
  /** Hiding a control is UX, not authorization (06 §8.3) — the API rejects
   *  the action independently. This only keeps the nav honest. */
  perm?: Perm;
  /** Shown as a count badge; resolved by the shell. */
  badgeKey?: "live";
}

export interface NavSection {
  key: string;
  label: string;
  items: NavItem[];
}

/**
 * The sidebar. Grouped rather than a flat list of eleven links: at eleven
 * items a flat rail becomes a wall, and the groups match how the work
 * actually divides — what happened, what is booked, and how the assistant is
 * set up.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    key: "overview",
    label: "Overview",
    items: [
      { key: "dashboard", label: "Dashboard", href: "/", icon: LayoutDashboard },
      {
        key: "conversations",
        label: "Conversations",
        href: "/conversations",
        icon: MessagesSquare,
        perm: "conversations:read",
        badgeKey: "live",
      },
      { key: "analytics", label: "Analytics", href: "/analytics", icon: BarChart3, perm: "usage:read" },
    ],
  },
  {
    key: "operations",
    label: "Operations",
    items: [
      { key: "calendar", label: "Calendar", href: "/calendar", icon: CalendarRange, perm: "bookings:read" },
      { key: "bookings", label: "Bookings", href: "/bookings", icon: CalendarDays, perm: "bookings:read" },
      { key: "services", label: "Services", href: "/services", icon: Sparkles },
      { key: "hours", label: "Hours", href: "/hours", icon: Clock },
    ],
  },
  {
    key: "assistant",
    label: "Assistant",
    items: [
      { key: "agent", label: "AI Config", href: "/agent", icon: Bot, perm: "agents:read" },
      { key: "knowledge", label: "Knowledge", href: "/knowledge", icon: BookOpen, perm: "knowledge:read" },
      { key: "channels", label: "Channels", href: "/channels", icon: Radio, perm: "channels:read" },
    ],
  },
  {
    key: "administration",
    label: "Administration",
    items: [
      { key: "users", label: "Users", href: "/users", icon: Users, perm: "members:read" },
      { key: "audit", label: "Audit", href: "/audit", icon: ScrollText, perm: "audit:read" },
    ],
  },
];

export const ALL_NAV_ITEMS: NavItem[] = NAV_SECTIONS.flatMap((s) => s.items);

/** Icons used by the shell chrome rather than the nav itself. */
export const ShellIcons = { Activity };
