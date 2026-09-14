import type { Metadata } from "next";

import { LivePage } from "@/components/live/live-page";

export const metadata: Metadata = { title: "Live Twin" };

export default function Page() {
  return <LivePage />;
}
