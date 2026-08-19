"use client";

import { AlertTriangle, Check, Info, X, XCircle } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export type ToastVariant = "default" | "success" | "warning" | "destructive";

export interface ToastMessage {
  id: number;
  title: string;
  description?: string;
  variant?: ToastVariant;
}

type ToastInput = Omit<ToastMessage, "id">;

const ToastContext = React.createContext<((t: ToastInput) => void) | null>(null);

/** Module-level bridge so non-React callers (mutation handlers) can toast. */
let emit: ((t: ToastInput) => void) | null = null;

export function toast(t: ToastInput): void {
  emit?.(t);
}

const ICONS: Record<ToastVariant, React.ComponentType<{ className?: string }>> = {
  default: Info,
  success: Check,
  warning: AlertTriangle,
  destructive: XCircle,
};

const TONES: Record<ToastVariant, string> = {
  default: "border-line",
  success: "border-ok/40",
  warning: "border-warn/40",
  destructive: "border-danger/40",
};

const ICON_TONES: Record<ToastVariant, string> = {
  default: "text-info-fg",
  success: "text-ok-fg",
  warning: "text-warn-fg",
  destructive: "text-danger-fg",
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<ToastMessage[]>([]);
  const nextId = React.useRef(1);

  const push = React.useCallback((t: ToastInput) => {
    const id = nextId.current++;
    setToasts((prev) => [...prev, { ...t, id }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((x) => x.id !== id));
    }, 6000);
  }, []);

  React.useEffect(() => {
    emit = push;
    return () => {
      emit = null;
    };
  }, [push]);

  return (
    <ToastContext.Provider value={push}>
      {children}
      {/*
        role="status" + aria-live="polite" on the REGION, not on each toast:
        a live region must exist in the DOM before content is inserted into it,
        or screen readers miss the first announcement entirely.
      */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[calc(100vw-2rem)] max-w-sm flex-col gap-2"
      >
        {toasts.map((t) => {
          const variant = t.variant ?? "default";
          const Icon = ICONS[variant];
          return (
            <div
              key={t.id}
              className={cn(
                "pointer-events-auto flex items-start gap-3 rounded-card border bg-bg-raised p-4 shadow-overlay animate-toast-in",
                TONES[variant],
              )}
            >
              <Icon className={cn("mt-0.5 size-4 shrink-0", ICON_TONES[variant])} aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-fg">{t.title}</p>
                {t.description && <p className="mt-1 text-sm text-fg-muted">{t.description}</p>}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
                className="shrink-0 rounded-pill p-1 text-fg-subtle transition-colors duration-micro hover:bg-bg-inset hover:text-fg"
              >
                <X className="size-3.5" aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = React.useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx;
}
