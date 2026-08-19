import * as React from "react";
import { cn } from "../lib/cn";

export interface WaveformProps extends React.HTMLAttributes<HTMLDivElement> {
  /**
   * Real amplitudes, 0..1, one per bar. When omitted the waveform renders its
   * IDLE state: a flat line of minimum-height bars.
   *
   * DESIGN.md §5 is explicit — "when idle it is a flat line of minimum-height
   * bars, never a fake animation implying audio that isn't there". So there is
   * deliberately no `animate` prop: bars move only when given real data.
   */
  amplitudes?: number[];
  /** Bar count for the idle state. */
  bars?: number;
  /** Height of the strip in px. */
  height?: number;
  /** Accessible description; the waveform is otherwise decorative. */
  label?: string;
}

/**
 * Waveform — vertical bars, 3px wide, 2px gap, --accent, heights driven by
 * real amplitude (DESIGN.md §5).
 */
export const Waveform = React.forwardRef<HTMLDivElement, WaveformProps>(
  function Waveform(
    { className, amplitudes, bars = 48, height = 40, label, ...props },
    ref,
  ) {
    const idle = !amplitudes || amplitudes.length === 0;
    const values = idle ? new Array<number>(bars).fill(0) : amplitudes;

    return (
      <div
        ref={ref}
        className={cn("flex items-center gap-[2px] overflow-hidden", className)}
        style={{ height }}
        // Decorative unless labelled: a bar chart of audio levels conveys
        // nothing a screen reader can use, and the transcript carries meaning.
        {...(label
          ? { role: "img", "aria-label": label }
          : { "aria-hidden": "true" })}
        {...props}
      >
        {values.map((amplitude, i) => {
          // Minimum 2px so idle bars form a visible flat line rather than
          // disappearing; otherwise scale into the available height.
          const clamped = Math.max(0, Math.min(1, amplitude));
          const barHeight = Math.max(2, Math.round(clamped * height));
          return (
            <span
              key={i}
              className="w-[3px] shrink-0 rounded-pill bg-accent"
              style={{
                height: barHeight,
                // Idle bars sit dimmer so the strip reads as "no audio yet"
                // without implying activity.
                opacity: idle ? 0.35 : 0.55 + clamped * 0.45,
              }}
            />
          );
        })}
      </div>
    );
  },
);
