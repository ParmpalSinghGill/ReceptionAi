"use client";

import { useEffect } from "react";

import { Button } from "@/components/ui/button";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // In the real build this reports to Sentry. The digest is the only safe
    // thing to show a user — the message may contain internals.
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-dvh items-center justify-center px-4 py-12">
      <div className="w-full max-w-md space-y-4 rounded-card border border-line bg-bg-raised p-6">
        <h1 className="font-display text-page-title text-fg">Something went wrong</h1>
        <p className="text-sm text-fg-muted">
          This screen failed to load. Your data is safe — nothing was changed.
        </p>
        {error.digest && (
          <p className="font-mono text-xs text-fg-subtle">Reference: {error.digest}</p>
        )}
        <div className="flex gap-3">
          <Button variant="primary" dot onClick={reset}>
            Try again
          </Button>
          <Button variant="secondary" onClick={() => (window.location.href = "/")}>
            Back to the dashboard
          </Button>
        </div>
      </div>
    </div>
  );
}
