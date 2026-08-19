"use client";

import { forwardRef } from "react";

export const ErrorSummary = forwardRef<
  HTMLDivElement,
  {
    count: number;
    errors: Array<{ key: string; label: string; message: string }>;
    onJump: (key: string) => void;
  }
>(function ErrorSummary({ count, errors, onJump }, ref) {
  if (count === 0) return null;

  return (
    <div
      ref={ref}
      // tabIndex -1 makes it programmatically focusable without adding it to
      // the tab order. role="alert" makes the announcement immediate — this is
      // the one place "assertive" is correct, because the user just pressed
      // submit and is waiting for exactly this answer.
      tabIndex={-1}
      role="alert"
      className="rounded-card border border-danger/40 bg-danger-dim p-4 focus:outline-none focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-danger"
    >
      <h2 className="font-medium text-danger-fg">
        There {count === 1 ? "is 1 problem" : `are ${count} problems`} to fix
      </h2>
      <ul className="mt-2 space-y-1 text-sm">
        {errors.map((e) => (
          <li key={e.key}>
            <button
              type="button"
              className="text-left text-danger-fg underline underline-offset-2"
              onClick={() => onJump(e.key)}
            >
              {e.label}: {e.message}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
});
