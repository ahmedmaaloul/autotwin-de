"use client";

import { Copy, ExternalLink, FileText } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import { repositoryUrl } from "./doc-entries";

/**
 * One document: what it is, what it covers, and where it lives.
 *
 * When `NEXT_PUBLIC_REPO_URL` is set the whole card is a link; when it is not, the card is inert
 * and the path can be copied instead. Rendering a link that 404s because the deployment does not
 * know its own repository would be worse than showing the path plainly.
 */
export function DocLink({
  title,
  description,
  path,
  className,
}: {
  title: string;
  description?: string;
  path: string;
  className?: string;
}) {
  const t = useTranslations();
  const href = repositoryUrl(path);

  const body = (
    <>
      <FileText className="text-muted-foreground mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0">
        <p className="flex items-center gap-1.5 text-sm font-medium">
          {title}
          {href ? <ExternalLink className="text-muted-foreground size-3" aria-hidden /> : null}
        </p>
        {description ? (
          <p className="text-muted-foreground mt-0.5 text-xs leading-relaxed">{description}</p>
        ) : null}
        <p className="text-muted-foreground mt-1.5 truncate font-mono text-[0.6875rem]">{path}</p>
      </div>
    </>
  );

  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noreferrer noopener"
        className={cn(
          "border-border bg-card hover:border-primary/40 hover:bg-muted/40 focus-visible:ring-ring grid grid-cols-[auto_1fr] gap-x-2.5 rounded border px-3 py-2.5 transition-colors focus-visible:ring-2 focus-visible:outline-none",
          className,
        )}
      >
        {body}
        {/* The icon says "external" to a sighted reader; this says it to everyone else. */}
        <span className="sr-only">{t.docs.openInRepository}</span>
      </a>
    );
  }

  return (
    <div
      className={cn(
        "border-border bg-card relative grid grid-cols-[auto_1fr] gap-x-2.5 rounded border px-3 py-2.5",
        className,
      )}
    >
      {body}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="absolute top-1.5 right-1.5 size-7"
        aria-label={`${t.docs.copyPath}: ${path}`}
        onClick={() => {
          void navigator.clipboard?.writeText(path).then(
            () => toast.success(t.docs.pathCopied, { description: path }),
            () => undefined,
          );
        }}
      >
        <Copy className="size-3.5" aria-hidden />
      </Button>
    </div>
  );
}
