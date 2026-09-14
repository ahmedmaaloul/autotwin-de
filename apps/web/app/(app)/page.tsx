import type { Metadata } from "next";

import { OverviewPage } from "@/components/dashboard/overview-page";

export const metadata: Metadata = { title: "Übersicht" };

export default function Page() {
  return <OverviewPage />;
}
