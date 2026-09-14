import type { Metadata } from "next";

import { AnalyticsPage } from "@/components/analytics/analytics-page";

export const metadata: Metadata = { title: "Analysen" };

export default function Page() {
  return <AnalyticsPage />;
}
