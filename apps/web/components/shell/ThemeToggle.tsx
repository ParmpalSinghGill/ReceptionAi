"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "rai-theme";

/**
 * Applies the choice by stamping `[data-theme]` on <html>.
 *
 * The stamp must WIN IN BOTH DIRECTIONS (DESIGN.md §1): a user on an OS-dark
 * machine who picks light gets light, and vice versa. tokens.css does that
 * with `@media (prefers-color-scheme: light) :root:not([data-theme="dark"])`
 * plus an explicit `:root[data-theme="light"]` block. "system" removes the
 * attribute entirely so the media query is the only thing deciding — setting
 * data-theme to the *resolved* value instead would freeze the page against a
 * later OS change.
 */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

/**
 * The blocking script that runs before paint.
 *
 * This is the one place a render-blocking inline script is correct: without
 * it, a user whose stored choice differs from the OS preference sees a flash
 * of the wrong theme on every navigation.
 */
export const THEME_INIT_SCRIPT = `
(function(){try{var c=localStorage.getItem(${JSON.stringify(STORAGE_KEY)});
if(c==="light"||c==="dark"){document.documentElement.setAttribute("data-theme",c);}}catch(e){}})();
`;

const OPTIONS: Array<{ value: ThemeChoice; label: string; icon: typeof Sun }> = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Monitor },
];

export function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>("system");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    try {
      const stored = localStorage.getItem(STORAGE_KEY) as ThemeChoice | null;
      if (stored === "light" || stored === "dark") setChoice(stored);
    } catch {
      /* private mode; system default is a fine answer */
    }
  }, []);

  const select = (next: ThemeChoice) => {
    setChoice(next);
    applyTheme(next);
    try {
      if (next === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* the theme still applies for this session */
    }
  };

  // Before hydration the stored choice is unknown, so render the neutral
  // system icon rather than guessing and flipping on mount.
  const Active = mounted ? (OPTIONS.find((o) => o.value === choice)?.icon ?? Monitor) : Monitor;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex size-9 items-center justify-center rounded-pill text-fg-muted transition-colors duration-micro hover:bg-bg-inset hover:text-fg"
        aria-label={`Theme: ${mounted ? choice : "system"}. Change theme`}
      >
        <Active className="size-4" aria-hidden="true" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        {OPTIONS.map(({ value, label, icon: Icon }) => (
          <DropdownMenuItem key={value} onSelect={() => select(value)}>
            <Icon aria-hidden="true" />
            <span className="flex-1">{label}</span>
            {/* The tick is the redundant channel — selection is never carried
                by highlight colour alone. */}
            {mounted && choice === value && (
              <span className={cn("text-xs text-accent")} aria-label="(selected)">
                ✓
              </span>
            )}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
