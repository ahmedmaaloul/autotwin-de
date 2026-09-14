import type { Metadata } from "next";

import { SystemPage } from "@/components/system/system-page";

export const metadata: Metadata = { title: "Systemstatus" };

export default function Page() {
  return <SystemPage />;
}
