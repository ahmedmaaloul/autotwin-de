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
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { DEMO_ROUTE_SLUG } from "@/lib/constants";
import { useTranslations } from "@/providers/locale-provider";

/** ⌘K / Ctrl-K opens the palette from anywhere. */
export function useGlobalSearch() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen((previous) => !previous);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  return { open, setOpen };
}

export function GlobalSearch({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useTranslations();
  const router = useRouter();

  const go = useCallback(
    (href: string) => {
      onOpenChange(false);
      router.push(href);
    },
    [onOpenChange, router],
  );

  const pages = [
    { href: "/", icon: LayoutDashboard, label: t.nav.overview },
    { href: "/live", icon: Activity, label: t.nav.live },
    { href: "/routes", icon: Route, label: t.nav.routes },
    { href: "/charging", icon: Zap, label: t.nav.charging },
    { href: "/analytics", icon: ChartNoAxesCombined, label: t.nav.analytics },
    { href: "/simulation", icon: CircuitBoard, label: t.nav.simulation },
    { href: "/ml", icon: BrainCircuit, label: t.nav.ml },
    { href: "/data-quality", icon: ShieldCheck, label: t.nav.dataQuality },
    { href: "/system", icon: ServerCog, label: t.nav.system },
    { href: "/docs", icon: BookOpen, label: t.nav.docs },
  ];

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title={t.common.search}
      description={t.common.searchPlaceholder}
    >
      <CommandInput placeholder={t.common.searchPlaceholder} />
      <CommandList>
        <CommandEmpty>{t.common.noResults}</CommandEmpty>
        <CommandGroup heading={t.nav.sectionPlatform}>
          {pages.map((page) => (
            <CommandItem key={page.href} value={page.label} onSelect={() => go(page.href)}>
              <page.icon className="size-4" aria-hidden />
              {page.label}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading={t.overview.demoCorridor}>
          <CommandItem
            value="Frankfurt am Main Stuttgart A5 A8"
            onSelect={() => go(`/routes?route=${DEMO_ROUTE_SLUG}`)}
          >
            <Route className="size-4" aria-hidden />
            Frankfurt am Main → Stuttgart
          </CommandItem>
          <CommandItem
            value="Schnellladepunkte fast chargers"
            onSelect={() => go("/charging?fast=true&minPower=150")}
          >
            <Zap className="size-4" aria-hidden />
            {t.charging.categoryUltraFast} ≥ 150 kW
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}
