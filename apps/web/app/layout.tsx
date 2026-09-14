import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from "next/font/google";
import { cookies } from "next/headers";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { LOCALE_COOKIE, LOCALE_TAGS, resolveLocale } from "@/lib/i18n/config";
import { LocaleProvider } from "@/providers/locale-provider";
import { QueryProvider } from "@/providers/query-provider";
import { ThemeProvider } from "@/providers/theme-provider";

import "./globals.css";

/**
 * IBM Plex — a rationalist family built on a grid for technical documentation. Chosen over the
 * Inter/Geist default because this product is dense with measurements: Plex Mono carries every
 * number, Plex Sans Condensed carries the dense table and section labels, and the three cuts
 * read as one system. See docs/DESIGN_SYSTEM.md §3.
 */
const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin", "latin-ext"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
  fallback: ["ui-sans-serif", "system-ui", "Segoe UI", "sans-serif"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin", "latin-ext"],
  weight: ["400", "500", "600"],
  display: "swap",
  fallback: ["ui-monospace", "SFMono-Regular", "monospace"],
});

const plexCondensed = IBM_Plex_Sans_Condensed({
  variable: "--font-plex-condensed",
  subsets: ["latin", "latin-ext"],
  weight: ["500", "600"],
  display: "swap",
  fallback: ["ui-sans-serif", "system-ui", "sans-serif"],
});

export const metadata: Metadata = {
  title: {
    default: "AutoTwin DE — Digitaler Zwilling der Elektromobilität",
    template: "%s · AutoTwin DE",
  },
  description:
    "Digitaler Zwilling der deutschen Elektromobilität: offizielle Ladeinfrastruktur, Wetter- und Verkehrsdaten, simulierte Fahrzeugtelemetrie, Energieprognose und Ladeoptimierung.",
  applicationName: "AutoTwin DE",
  authors: [{ name: "AutoTwin DE" }],
  keywords: [
    "Elektromobilität",
    "Ladeinfrastruktur",
    "Digitaler Zwilling",
    "Bundesnetzagentur",
    "Autobahn",
    "PostGIS",
  ],
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FAFAFA" },
    { media: "(prefers-color-scheme: dark)", color: "#0C0E12" },
  ],
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // The locale cookie is read on the server so `<html lang>` is correct on the first paint —
  // no flash of the wrong language, and screen readers get the right pronunciation immediately.
  const store = await cookies();
  const locale = resolveLocale(store.get(LOCALE_COOKIE)?.value);

  return (
    <html
      lang={LOCALE_TAGS[locale]}
      className={`${plexSans.variable} ${plexMono.variable} ${plexCondensed.variable} h-full`}
      suppressHydrationWarning
    >
      <body className="bg-background text-foreground min-h-full font-sans antialiased">
        <ThemeProvider>
          <LocaleProvider locale={locale}>
            <QueryProvider>
              <TooltipProvider delayDuration={200}>
                {children}
                <Toaster position="bottom-right" />
              </TooltipProvider>
            </QueryProvider>
          </LocaleProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
