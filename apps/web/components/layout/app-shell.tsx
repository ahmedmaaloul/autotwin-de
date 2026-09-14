import type { ReactNode } from "react";

import { AppSidebar } from "@/components/layout/app-sidebar";
import { AppTopbar } from "@/components/layout/app-topbar";
import { SnapshotBanner } from "@/components/layout/snapshot-banner";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

/**
 * Desktop-first engineering shell: a persistent sidebar, a status-bearing top bar, and a main
 * column that scrolls independently so the top bar stays visible while reading a long table.
 */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset className="min-w-0">
        <AppTopbar />
        <SnapshotBanner />
        <main className="min-w-0 flex-1 px-4 py-4 md:px-6 md:py-6">{children}</main>
      </SidebarInset>
    </SidebarProvider>
  );
}
