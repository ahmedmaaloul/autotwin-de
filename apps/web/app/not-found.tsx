import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="flex min-h-svh flex-col items-center justify-center gap-4 px-6 text-center">
      <p className="eyebrow">404</p>
      <h1 className="text-xl font-semibold tracking-tight">Seite nicht gefunden</h1>
      <p className="text-muted-foreground max-w-sm text-sm">
        Diese Seite existiert nicht. Zurück zur Übersicht der Plattform.
      </p>
      <Button asChild size="sm">
        <Link href="/">Zur Übersicht</Link>
      </Button>
    </main>
  );
}
