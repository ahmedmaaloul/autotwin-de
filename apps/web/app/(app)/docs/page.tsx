import type { Metadata } from "next";

import { DocsIndex } from "./docs-index";

export const metadata: Metadata = { title: "Dokumentation" };

export default function Page() {
  return <DocsIndex />;
}
