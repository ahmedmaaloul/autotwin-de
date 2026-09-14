import type { Metadata } from "next";
import { Suspense } from "react";

import { ChargingPage } from "@/components/charging/charging-page";
import { LoadingSkeleton } from "@/components/shared/states";

export const metadata: Metadata = { title: "Ladeinfrastruktur" };

/**
 * `/charging`.
 *
 * The explorer reads its entire state from the URL, which means it calls `useSearchParams()`
 * and therefore must sit inside a Suspense boundary — without one, Next would opt the whole
 * route out of static rendering at build time.
 */
export default function Page() {
  return (
    <Suspense fallback={<LoadingSkeleton rows={8} />}>
      <ChargingPage />
    </Suspense>
  );
}
