"use client";

import * as React from "react";
import { cn } from "../lib/cn";

export interface AvatarProps extends React.HTMLAttributes<HTMLSpanElement> {
  /** Image source. When absent or failed to load, initials are shown. */
  src?: string;
  /** The person or entity's name — drives initials and the accessible name. */
  name: string;
  size?: "sm" | "md" | "lg";
}

const SIZES = {
  sm: "size-6 text-[10px]",
  md: "size-8 text-[11px]",
  lg: "size-10 text-body-sm",
} as const;

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0]?.[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1]?.[0] ?? "") : "";
  return (first + last).toUpperCase();
}

/**
 * Avatar — image with an initials fallback.
 *
 * The wrapper carries the accessible name via `title`/aria-label rather than
 * the <img> alt, so the initials fallback is equally well described. The
 * decorative image itself uses alt="" to avoid announcing the name twice.
 */
export const Avatar = React.forwardRef<HTMLSpanElement, AvatarProps>(
  function Avatar({ className, src, name, size = "md", ...props }, ref) {
    const [failed, setFailed] = React.useState(false);
    const showImage = Boolean(src) && !failed;

    return (
      <span
        ref={ref}
        role="img"
        aria-label={name}
        className={cn(
          "inline-flex shrink-0 items-center justify-center overflow-hidden rounded-pill",
          "border border-line bg-accent-dim font-sans font-semibold text-accent",
          SIZES[size],
          className,
        )}
        {...props}
      >
        {showImage ? (
          <img
            src={src}
            alt=""
            className="size-full object-cover"
            onError={() => setFailed(true)}
          />
        ) : (
          <span aria-hidden="true">{initialsOf(name)}</span>
        )}
      </span>
    );
  },
);
