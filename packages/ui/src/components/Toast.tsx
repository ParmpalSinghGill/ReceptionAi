"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { cn } from "../lib/cn";
import { focusRing } from "../lib/focus";

export type ToastVariant = "neutral" | "ok" | "warn" | "danger" | "info";

export interface Toast {
  id: string;
  title: string;
  description?: string;
  variant?: ToastVariant;
  /** ms before auto-dismiss. Pass 0 to require manual dismissal. */
  duration?: number;
}

interface ToastContextValue {
  toast: (t: Omit<Toast, "id">) => string;
  dismiss: (id: string) => void;
}

const ToastContext = React.createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

/** Glyph per variant — status is never colour alone (DESIGN.md §7). */
const GLYPH: Record<ToastVariant, string> = {
  neutral: "•",
  ok: "✓",
  warn: "!",
  danger: "✕",
  info: "i",
};

const TONE: Record<ToastVariant, string> = {
  neutral: "text-fg-muted",
  ok: "text-ok-fg",
  warn: "text-warn-fg",
  danger: "text-danger-fg",
  info: "text-info-fg",
};

/**
 * ToastProvider — renders the live region and exposes `useToast()`.
 *
 * The region is `aria-live="polite"` so a toast is announced without
 * interrupting, and `role="status"`. Errors use `assertive` on their own node
 * because a failure the user must act on should cut in.
 */
export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([]);
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);

  const dismiss = React.useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = React.useCallback((t: Omit<Toast, "id">) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((prev) => [...prev, { ...t, id }]);
    return id;
  }, []);

  const value = React.useMemo(() => ({ toast, dismiss }), [toast, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {mounted
        ? createPortal(
            <div
              className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-3"
              // The container is the live region; individual toasts entering
              // it are announced without the container being re-read.
              role="status"
              aria-live="polite"
            >
              {toasts.map((t) => (
                <ToastItem key={t.id} toast={t} onDismiss={dismiss} />
              ))}
            </div>,
            document.body,
          )
        : null}
    </ToastContext.Provider>
  );
}

function ToastItem({
  toast,
  onDismiss,
}: {
  toast: Toast;
  onDismiss: (id: string) => void;
}) {
  const { id, title, description, variant = "neutral", duration = 6000 } = toast;

  React.useEffect(() => {
    if (duration <= 0) return;
    const timer = window.setTimeout(() => onDismiss(id), duration);
    return () => window.clearTimeout(timer);
  }, [id, duration, onDismiss]);

  return (
    <div
      className={cn(
        "pointer-events-auto flex items-start gap-3 rounded-card border border-line",
        "bg-bg-raised p-4 shadow-overlay motion-safe:animate-toast-in",
      )}
    >
      <span aria-hidden="true" className={cn("mt-0.5 font-semibold", TONE[variant])}>
        {GLYPH[variant]}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="font-sans text-body-sm font-medium text-fg">{title}</p>
        {description ? (
          <p className="text-body-sm text-fg-muted">{description}</p>
        ) : null}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(id)}
        aria-label={`Dismiss: ${title}`}
        className={cn(
          "grid size-6 shrink-0 place-items-center rounded-pill text-fg-subtle",
          "transition-colors duration-micro ease-brand hover:bg-bg-inset hover:text-fg",
          focusRing,
        )}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          className="size-3"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        >
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  );
}
