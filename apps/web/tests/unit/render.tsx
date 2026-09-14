import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Locale } from "@/lib/i18n/config";
import { LocaleProvider } from "@/providers/locale-provider";

/**
 * The provider stack every component in AutoTwin DE is rendered inside.
 *
 * `LocaleProvider` is not optional: `useTranslations()` throws without it, by design (see
 * providers/locale-provider.tsx), so a component test that forgets it fails for the wrong
 * reason. Locale is a parameter rather than a constant because half the value of these tests
 * is checking that German and English really do render differently.
 */
export interface RenderWithProvidersOptions extends Omit<RenderOptions, "wrapper"> {
  locale?: Locale;
}

/**
 * Retries and caching are deliberately off: a test that waits for an exponential backoff is a
 * test that occasionally times out in CI.
 */
function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderWithProviders(
  ui: ReactElement,
  { locale = "de", ...options }: RenderWithProvidersOptions = {},
): RenderResult {
  const client = createTestQueryClient();
  // The provider resolves the locale from the cookie — there is no server in a static export —
  // so a test picks a language exactly the way a browser does.
  document.cookie = `autotwin_locale=${locale}; path=/`;

  function Wrapper({ children }: { children: ReactNode }): ReactElement {
    return (
      <LocaleProvider>
        <QueryClientProvider client={client}>
          <TooltipProvider delayDuration={0}>{children}</TooltipProvider>
        </QueryClientProvider>
      </LocaleProvider>
    );
  }

  return render(ui, { wrapper: Wrapper, ...options });
}
