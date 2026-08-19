"use client";

import * as React from "react";
import { Button, cn } from "@receptionai/ui";
import { Wordmark } from "./Wordmark";

const LINKS = [
  { href: "#how-it-works", label: "How it works" },
  { href: "#industries", label: "Industries" },
  { href: "#why-it-works", label: "Why it works" },
  { href: "#faq", label: "FAQ" },
];

/**
 * Sticky nav. On small screens the section links collapse behind a disclosure
 * button rather than wrapping into a second row — the CTA stays visible at
 * every width, since it is the point of the page.
 */
export function SiteNav({ onRequestCall }: { onRequestCall: () => void }) {
  const [open, setOpen] = React.useState(false);

  return (
    <header className="sticky top-0 z-40 border-b border-line bg-bg-header/95 backdrop-blur">
      <nav
        aria-label="Main"
        className="mx-auto flex h-16 max-w-content items-center justify-between gap-4 px-step5"
      >
        <a
          href="#main"
          className="rounded-pill focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
          aria-label="ReceptionAi home"
        >
          <Wordmark />
        </a>

        <ul className="hidden items-center gap-step6 md:flex">
          {LINKS.map((l) => (
            <li key={l.href}>
              <a
                href={l.href}
                className="rounded-pill px-1 py-1 font-sans text-body-sm text-fg-muted transition-colors duration-micro hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2"
              >
                {l.label}
              </a>
            </li>
          ))}
        </ul>

        <div className="flex items-center gap-2">
          <Button size="sm" onClick={onRequestCall} className="hidden sm:inline-flex">
            Request a call
          </Button>

          <button
            type="button"
            className="grid size-9 place-items-center rounded-pill border border-line text-fg-muted transition-colors duration-micro hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 md:hidden"
            aria-expanded={open}
            aria-controls="nav-sections"
            aria-label={open ? "Close navigation" : "Open navigation"}
            onClick={() => setOpen((v) => !v)}
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              className="size-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            >
              {open ? (
                <path d="M4 4l8 8M12 4l-8 8" />
              ) : (
                <path d="M2 4.5h12M2 8h12M2 11.5h12" />
              )}
            </svg>
          </button>
        </div>
      </nav>

      {/* Rendered but hidden rather than unmounted, so the aria-controls
          relationship always points at a real element. */}
      <div
        id="nav-sections"
        hidden={!open}
        className="border-t border-line md:hidden"
      >
        <ul className="mx-auto flex max-w-content flex-col px-step5 py-2">
          {LINKS.map((l) => (
            <li key={l.href}>
              <a
                href={l.href}
                onClick={() => setOpen(false)}
                className={cn(
                  "block rounded-input px-2 py-3 font-sans text-body-sm text-fg-muted",
                  "transition-colors duration-micro hover:bg-bg-raised hover:text-fg",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                )}
              >
                {l.label}
              </a>
            </li>
          ))}
          <li className="pb-2 pt-2 sm:hidden">
            <Button size="sm" onClick={onRequestCall} className="w-full">
              Request a call
            </Button>
          </li>
        </ul>
      </div>
    </header>
  );
}
