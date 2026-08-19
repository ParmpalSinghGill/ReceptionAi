"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

const FOCUSABLE =
  'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Accessible title. Rendered as the heading and wired via aria-labelledby. */
  title: string;
  /** Optional supporting copy, wired via aria-describedby. */
  description?: string;
  children?: React.ReactNode;
  /** Footer actions, typically buttons. */
  footer?: React.ReactNode;
  className?: string;
}

/**
 * Dialog — a modal with the three things a modal must get right:
 *   1. Focus moves IN on open and is RESTORED to the trigger on close.
 *   2. Tab is trapped inside while open (cycling at both ends).
 *   3. Escape closes, and the backdrop click closes.
 * Plus `aria-modal` and a labelled heading so it is announced as a dialog.
 *
 * Body scroll is locked while open, otherwise the page behind scrolls under
 * the overlay on touch devices.
 */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  className,
}: DialogProps) {
  const panelRef = React.useRef<HTMLDivElement>(null);
  const restoreRef = React.useRef<HTMLElement | null>(null);
  const titleId = React.useId();
  const descId = React.useId();
  // Portals need the document, which does not exist during SSR.
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  React.useEffect(() => {
    if (!open) return;

    // Remember what had focus so it can be handed back on close.
    restoreRef.current = document.activeElement as HTMLElement | null;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    // Focus the first focusable node, falling back to the panel itself.
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel)?.focus();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onOpenChange(false);
        return;
      }
      if (e.key !== "Tab") return;

      const nodes = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [],
      ).filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (nodes.length === 0) {
        e.preventDefault();
        return;
      }
      const firstNode = nodes[0]!;
      const lastNode = nodes[nodes.length - 1]!;
      // Wrap at both ends so focus can never escape to the page behind.
      if (e.shiftKey && document.activeElement === firstNode) {
        e.preventDefault();
        lastNode.focus();
      } else if (!e.shiftKey && document.activeElement === lastNode) {
        e.preventDefault();
        firstNode.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      restoreRef.current?.focus?.();
    };
  }, [open, onOpenChange]);

  if (!open || !mounted) return null;

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-scrim motion-safe:animate-overlay-in"
        // The backdrop is a click target only; Escape covers the keyboard
        // path, so it needs no role of its own.
        onClick={() => onOpenChange(false)}
        aria-hidden="true"
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        className={cn(
          "relative w-full max-w-lg rounded-card border border-line bg-bg-raised p-step5 shadow-overlay",
          "motion-safe:animate-dialog-in outline-none",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2
              id={titleId}
              className="font-display text-page-title text-fg"
            >
              {title}
            </h2>
            {description ? (
              <p id={descId} className="text-body-sm text-fg-muted">
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            aria-label="Close dialog"
            className={cn(
              "grid size-8 shrink-0 place-items-center rounded-pill text-fg-muted",
              "transition-colors duration-micro ease-brand hover:bg-bg-inset hover:text-fg",
              focusRing,
            )}
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
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        </div>

        {children ? <div className="mt-4">{children}</div> : null}
        {footer ? (
          <div className="mt-step5 flex items-center justify-end gap-3">{footer}</div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
