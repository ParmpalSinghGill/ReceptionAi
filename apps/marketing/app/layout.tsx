import type { Metadata } from "next";
import { Inter, Instrument_Serif, JetBrains_Mono } from "next/font/google";
import "./globals.css";

/**
 * The serif/sans pairing IS the personality (DESIGN.md §2). Instrument Serif
 * ships a true italic, which the hero's emphasis word requires — DESIGN.md
 * bans faux-italic, so the italic face is loaded explicitly.
 */
const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-instrument-serif",
  display: "swap",
});

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "ReceptionAi — the front desk that always picks up",
  description:
    "ReceptionAi answers every call, text and web chat for clinics, clinics and trades — books the appointment, writes it to your calendar, and hands you the transcript.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    // The marketing site commits to the designed dark appearance rather than
    // following the OS, so the screenshot is what every visitor sees.
    // suppressHydrationWarning because the attribute is authored here and the
    // /preview page flips themes on subtrees below it.
    <html
      lang="en"
      data-theme="dark"
      suppressHydrationWarning
      className={`${instrumentSerif.variable} ${inter.variable} ${jetbrainsMono.variable}`}
    >
      <body>
        {/* WCAG 2.4.1: a keyboard user must be able to jump past the nav. */}
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-pill focus:bg-accent focus:px-step5 focus:py-3 focus:font-sans focus:text-body-sm focus:font-medium focus:text-accent-fg"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
