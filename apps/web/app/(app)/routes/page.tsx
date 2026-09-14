import type { Metadata } from "next";
import { Suspense } from "react";

import { AnalysisSkeleton } from "@/components/routes/analysis-skeleton";
import { RoutesPage } from "@/components/routes/routes-page";

export const metadata: Metadata = {
  title: "Routen",
  description:
    "Streckenanalyse mit Energiebedarfsprognose, Ladezustandsverlauf und Ladeplanung entlang deutscher Korridore.",
};

/**
 * `/routes` reads its request out of the URL search params, and `useSearchParams()` opts a
 * client component out of static prerendering unless it sits under a Suspense boundary — so the
 * boundary is here, falling back to the same skeleton the analysis itself waits behind.
 */
export default function Page() {
  return (
    <Suspense fallback={<AnalysisSkeleton />}>
      <RoutesPage />
    </Suspense>
  );
}
