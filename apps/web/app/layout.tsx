import type { Metadata, Viewport } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, IBM_Plex_Sans_Condensed } from "next/font/google";

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DEFAULT_LOCALE, LOCALE_TAGS } from "@/lib/i18n/config";
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

export default function RootLayout({ children }: LayoutProps<"/">) {
  // `lang` is rendered as the default and corrected on the client by LocaleProvider once the
  // cookie is read. Resolving it here would need a request, and the hosted demo is a fully
  // static export with no server — the same layout has to work in both worlds.
  return (
    <html
      lang={LOCALE_TAGS[DEFAULT_LOCALE]}
      className={`${plexSans.variable} ${plexMono.variable} ${plexCondensed.variable} h-full`}
      suppressHydrationWarning
    >
      <body className="bg-background text-foreground min-h-full font-sans antialiased">
        <ThemeProvider>
          <LocaleProvider>
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
