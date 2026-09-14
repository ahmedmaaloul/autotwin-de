"use client";

import { AlertTriangle, ExternalLink, Info } from "lucide-react";

import { NoticePanel } from "@/components/analytics/notice-panel";
import { PageHeader, SectionHeader } from "@/components/shared/section-header";
import { Card } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useTranslations } from "@/providers/locale-provider";

import {
  ADR_DOCS,
  ADR_INDEX,
  DATA_DOCS,
  DATA_SOURCES,
  LICENCE_DOCS,
  SPEC_DOCS,
  repositoryUrl,
} from "./doc-entries";
import { DocLink } from "./doc-link";

/**
 * An index of the project's written record — not a viewer.
 *
 * Rendering Markdown in the app would duplicate documents that are already versioned beside the
 * code and would go stale the first time someone edited one. What this page owes the reader is
 * different: a map of what exists, one line on why each document is worth opening, and — the
 * part that is not optional — the licences and attributions the ingested data carries with it
 * (DATA_LICENSES.md).
 */
export function DocsIndex() {
  const t = useTranslations();
  const hasRepository = repositoryUrl("README.md") !== undefined;

  return (
    <div className="space-y-6">
      <PageHeader title={t.docs.title} description={t.docs.subtitle} />

      <NoticePanel icon={Info} title={t.docs.title}>
        {t.docs.intro}
        {hasRepository ? null : <span className="mt-1 block">{t.docs.repositoryHint}</span>}
      </NoticePanel>

      <section className="space-y-3">
        <SectionHeader eyebrow={t.nav.docs} title={t.docs.sectionSpec} />
        <div className="grid gap-3 md:grid-cols-2">
          {SPEC_DOCS.map((entry) => (
            <DocLink
              key={entry.path}
              path={entry.path}
              title={t.docs.items[entry.key].title}
              description={t.docs.items[entry.key].description}
            />
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <SectionHeader eyebrow={t.nav.docs} title={t.docs.sectionData} />
        <div className="grid gap-3 md:grid-cols-2">
          {DATA_DOCS.map((entry) => (
            <DocLink
              key={entry.path}
              path={entry.path}
              title={t.docs.items[entry.key].title}
              description={t.docs.items[entry.key].description}
            />
          ))}
        </div>
      </section>

      <section className="space-y-3">
        <SectionHeader
          eyebrow={t.nav.docs}
          title={t.docs.sectionAdr}
          description={t.docs.sectionAdrIntro}
        />
        <DocLink
          path={ADR_INDEX.path}
          title={t.docs.items[ADR_INDEX.key].title}
          description={t.docs.items[ADR_INDEX.key].description}
        />
        <Card className="gap-0 rounded border p-0 shadow-none">
          <ul className="divide-border divide-y">
            {ADR_DOCS.map((entry) => {
              const href = repositoryUrl(entry.path);
              const label = t.docs.adr[entry.key];
              return (
                <li key={entry.id}>
                  <AdrRow id={entry.id} label={label} href={href} path={entry.path} />
                </li>
              );
            })}
          </ul>
        </Card>
      </section>

      <section className="space-y-3">
        <SectionHeader
          eyebrow={t.dataQuality.licence}
          title={t.docs.sectionLicence}
          description={t.docs.licenceIntro}
        />

        <Card className="gap-0 rounded border p-0 shadow-none">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.docs.columnSource}</TableHead>
                  <TableHead>{t.docs.columnContent}</TableHead>
                  <TableHead>{t.docs.columnLicence}</TableHead>
                  <TableHead>{t.docs.columnAttribution}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {DATA_SOURCES.map((source) => {
                  const entry = t.docs.sources[source.key];
                  return (
                    <TableRow key={source.key}>
                      <TableCell className="align-top font-medium whitespace-nowrap">
                        <a
                          href={source.url}
                          target="_blank"
                          rel="noreferrer noopener"
                          className="text-primary inline-flex items-center gap-1 underline underline-offset-3"
                        >
                          {entry.name}
                          <ExternalLink className="size-3" aria-hidden />
                        </a>
                      </TableCell>
                      <TableCell className="text-muted-foreground align-top text-xs">
                        {entry.content}
                      </TableCell>
                      <TableCell className="align-top font-mono text-xs whitespace-nowrap">
                        {source.licenceUrl ? (
                          <a
                            href={source.licenceUrl}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="underline underline-offset-3"
                          >
                            {entry.licence}
                          </a>
                        ) : (
                          // Signalgelb on white is 3.8:1 — enough for an icon, not for a word,
                          // so the icon carries the colour and the text keeps AA contrast.
                          <span className="inline-flex items-center gap-1">
                            <AlertTriangle className="text-warning size-3" aria-hidden />
                            {t.docs.notDeclared}
                          </span>
                        )}
                      </TableCell>
                      <TableCell className="align-top text-xs">
                        <span className="border-border bg-muted rounded border px-1.5 py-0.5">
                          {entry.attribution}
                        </span>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          <p className="border-border text-muted-foreground border-t px-4 py-2.5 text-xs leading-relaxed">
            {t.docs.autobahnCaveat}
          </p>
        </Card>

        <div className="grid gap-3 md:grid-cols-2">
          {LICENCE_DOCS.map((entry) => (
            <DocLink
              key={entry.path}
              path={entry.path}
              title={t.docs.items[entry.key].title}
              description={t.docs.items[entry.key].description}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

function AdrRow({
  id,
  label,
  href,
  path,
}: {
  id: string;
  label: string;
  href: string | undefined;
  path: string;
}) {
  const content = (
    <>
      <span className="text-muted-foreground w-8 shrink-0 font-mono text-xs tabular-nums">
        {id}
      </span>
      <span className="min-w-0 flex-1 truncate text-sm">{label}</span>
      {href ? <ExternalLink className="text-muted-foreground size-3 shrink-0" aria-hidden /> : null}
    </>
  );

  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        className="hover:bg-muted/50 focus-visible:ring-ring flex items-center gap-3 px-4 py-2 transition-colors focus-visible:ring-2 focus-visible:outline-none focus-visible:-outline-offset-2"
      >
        {content}
      </a>
    );
  }

  return (
    <div className="flex items-center gap-3 px-4 py-2" title={path}>
      {content}
    </div>
  );
}
