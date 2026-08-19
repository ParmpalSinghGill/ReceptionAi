"use client";

import * as SliderPrimitive from "@radix-ui/react-slider";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Slider = React.forwardRef<
  React.ElementRef<typeof SliderPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root>
>(function Slider({ className, ...props }, ref) {
  return (
    <SliderPrimitive.Root
      ref={ref}
      className={cn("relative flex w-full touch-none select-none items-center py-2", className)}
      {...props}
    >
      <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-pill bg-bg-inset border border-line">
        <SliderPrimitive.Range className="absolute h-full bg-accent" />
      </SliderPrimitive.Track>
      {/* 20px thumb with a 24px hit area via padding on the root — clears 2.5.8. */}
      <SliderPrimitive.Thumb className="block size-5 rounded-pill border-2 border-accent bg-bg-raised transition-colors duration-micro disabled:pointer-events-none disabled:opacity-50" />
    </SliderPrimitive.Root>
  );
});
