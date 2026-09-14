import type { Metadata } from "next";

import { DataQualityPage } from "@/components/data-quality/data-quality-page";

export const metadata: Metadata = { title: "Datenqualität" };

export default function Page() {
  return <DataQualityPage />;
}
