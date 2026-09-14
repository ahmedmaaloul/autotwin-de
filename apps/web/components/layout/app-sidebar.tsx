"use client";

import {
  Activity,
  BookOpen,
  BrainCircuit,
  ChartNoAxesCombined,
  CircuitBoard,
  LayoutDashboard,
  Route,
  ServerCog,
  ShieldCheck,
  Zap,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import type { Messages } from "@/lib/i18n/messages/de";
import { useTranslations } from "@/providers/locale-provider";

interface NavItem {
  href: string;
  icon: LucideIcon;
  label: (t: Messages) => string;
}

interface NavSection {
  label: (t: Messages) => string;
  items: NavItem[];
}

/**
 * Navigation is grouped by what the reader is doing, not by which service serves it:
 * *Plattform* is the operational picture, *Engineering* is the machinery behind it,
 * *System* is whether the machinery is healthy.
 */
const SECTIONS: NavSection[] = [
  {
    label: (t) => t.nav.sectionPlatform,
    items: [
      { href: "/", icon: LayoutDashboard, label: (t) => t.nav.overview },
      { href: "/live", icon: Activity, label: (t) => t.nav.live },
      { href: "/routes", icon: Route, label: (t) => t.nav.routes },
      { href: "/charging", icon: Zap, label: (t) => t.nav.charging },
    ],
  },
  {
    label: (t) => t.nav.sectionEngineering,
    items: [
      { href: "/analytics", icon: ChartNoAxesCombined, label: (t) => t.nav.analytics },
      { href: "/simulation", icon: CircuitBoard, label: (t) => t.nav.simulation },
      { href: "/ml", icon: BrainCircuit, label: (t) => t.nav.ml },
      { href: "/data-quality", icon: ShieldCheck, label: (t) => t.nav.dataQuality },
    ],
  },
  {
    label: (t) => t.nav.sectionSystem,
    items: [
      { href: "/system", icon: ServerCog, label: (t) => t.nav.system },
      { href: "/docs", icon: BookOpen, label: (t) => t.nav.docs },
    ],
  },
];

export function AppSidebar() {
  const t = useTranslations();
  const pathname = usePathname();

  return (
    <Sidebar collapsible="icon" className="border-border border-r">
      <SidebarHeader className="border-border h-14 justify-center border-b px-3">
        <Link
          href="/"
          className="focus-visible:ring-ring flex items-center gap-2.5 rounded-sm focus-visible:ring-2 focus-visible:outline-none"
        >
          <AutoTwinMark />
          <span className="grid min-w-0 group-data-[collapsible=icon]:hidden">
            <span className="truncate text-sm leading-tight font-semibold tracking-tight">
              {t.app.name}
            </span>
            <span className="text-muted-foreground truncate text-[0.6875rem] leading-tight">
              {t.app.shortTagline}
            </span>
          </span>
        </Link>
      </SidebarHeader>

      <SidebarContent className="scrollbar-thin">
        {SECTIONS.map((section) => (
          <SidebarGroup key={section.label(t)}>
            <SidebarGroupLabel className="eyebrow">{section.label(t)}</SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu>
                {section.items.map((item) => {
                  const label = item.label(t);
                  // `/` must match exactly or it would light up on every page.
                  const isActive =
                    item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
                  return (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton asChild isActive={isActive} tooltip={label}>
                        <Link href={item.href}>
                          <item.icon aria-hidden />
                          <span>{label}</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        ))}
      </SidebarContent>

      <SidebarFooter className="border-border border-t px-3 py-2 group-data-[collapsible=icon]:hidden">
        <p className="text-muted-foreground text-[0.6875rem] leading-relaxed">
          Offizielle Daten: Bundesnetzagentur, DWD, Autobahn GmbH.
          <br />
          Fahrzeugtelemetrie ist simuliert.
        </p>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}

/**
 * The mark: a route line crossing a charging pylon, drawn on the same 24-unit grid as the
 * Lucide icons beside it so the header optically aligns.
 */
function AutoTwinMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="text-primary size-6 shrink-0"
      fill="none"
      aria-hidden
      focusable="false"
    >
      <rect x="0.5" y="0.5" width="23" height="23" rx="3" className="stroke-primary/25" />
      <path
        d="M4 17.5c3.2 0 3.6-5 6.4-5s3.2 5 6.4 5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path d="M13.6 5.5 10 11.2h2.6L11.4 15l3.9-5.9h-2.7z" fill="currentColor" />
    </svg>
  );
}
