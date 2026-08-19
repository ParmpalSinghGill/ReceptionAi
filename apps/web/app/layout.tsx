import type { Metadata } from "next";
import { Inter, Instrument_Serif, JetBrains_Mono } from "next/font/google";

import { THEME_INIT_SCRIPT } from "@/components/shell/ThemeToggle";
import { ToastProvider } from "@/components/ui/toast";

import "./globals.css";

/**
 * DESIGN.md §2 — two families, and the contrast between them is the whole
 * personality. Instrument Serif is the high-contrast display serif with a
 * TRUE italic (never faux); Inter is the UI sans; JetBrains Mono carries ids,
 * phone numbers and JSON.
 */
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const instrumentSerif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
  variable: "--font-instrument-serif",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "ReceptionAi",
    template: "%s · ReceptionAi",
  },
  description: "The front desk that always picks up — admin portal.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en-GB"
      suppressHydrationWarning
      className={`${inter.variable} ${instrumentSerif.variable} ${jetbrainsMono.variable}`}
    >
      <head>
        {/* The one place a render-blocking inline script is correct: without
            it a user whose stored theme differs from their OS preference sees
            a flash of the wrong theme on every navigation. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}
