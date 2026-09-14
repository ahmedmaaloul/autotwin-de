import type { Metadata } from "next";

import { SimulationPage } from "@/components/simulation/simulation-page";

export const metadata: Metadata = { title: "Simulation" };

export default function Page() {
  return <SimulationPage />;
}
