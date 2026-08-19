"use client";

import * as SwitchPrimitive from "@radix-ui/react-switch";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(function Switch({ className, ...props }, ref) {
  return (
    <SwitchPrimitive.Root
      ref={ref}
      className={cn(
        // 44×24 — clears the 24×24 minimum target size (2.5.8) on its own.
        "peer inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-pill border-2 border-transparent transition-colors duration-standard ease-brand",
        "data-[state=checked]:bg-accent data-[state=unchecked]:bg-bg-inset data-[state=unchecked]:border-line",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        className={cn(
          "pointer-events-none block size-5 rounded-pill shadow-sm ring-0 transition-transform duration-standard ease-brand",
          "data-[state=checked]:translate-x-5 data-[state=checked]:bg-accent-fg",
          "data-[state=unchecked]:translate-x-0 data-[state=unchecked]:bg-fg-subtle",
        )}
      />
    </SwitchPrimitive.Root>
  );
});
