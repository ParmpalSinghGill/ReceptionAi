import { cn } from "@receptionai/ui";

/**
 * The wordmark: a waveform glyph, "Reception" in --fg, "Ai" in --accent.
 *
 * The bars echo the Waveform component's 3px/2px rhythm at small scale, so
 * the logo and the live call strip read as the same object.
 */
export function Wordmark({ className }: { className?: string }) {
  // Deliberately asymmetric so it reads as a captured moment of audio rather
  // than a decorative pattern.
  const bars = [6, 12, 18, 11, 20, 8, 13];

  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <span
        aria-hidden="true"
        className="flex h-5 items-center gap-[2px]"
      >
        {bars.map((h, i) => (
          <span
            key={i}
            className="w-[3px] rounded-pill bg-accent"
            style={{ height: h, opacity: 0.55 + (h / 20) * 0.45 }}
          />
        ))}
      </span>
      <span className="font-sans text-body font-semibold tracking-tight text-fg">
        Reception<span className="text-accent">Ai</span>
      </span>
    </span>
  );
}
