import type { Metadata } from "next";

import { MlPage } from "@/components/ml/ml-page";

export const metadata: Metadata = { title: "ML Lab" };

export default function Page() {
  return <MlPage />;
}
