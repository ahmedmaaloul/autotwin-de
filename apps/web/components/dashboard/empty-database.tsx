"use client";

import { Copy, DatabaseZap } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/shared/states";
import { useTranslations } from "@/providers/locale-provider";

const DEMO_COMMAND = "make demo";

/**
 * What an empty database should say.
 *
 * The generic "no data" panel is a dead end; the reader's actual next step is one command, so
 * the command is the content — selectable, copyable, and spelled exactly as the Makefile
 * target (BUILD_SPEC §15), not paraphrased.
 */
export function EmptyDatabaseState() {
  const t = useTranslations();

  const copy = () => {
    void navigator.clipboard
      .writeText(DEMO_COMMAND)
      .then(() => toast.success(t.common.copied))
      .catch(() => toast.error(t.states.errorTitle));
  };

  return (
    <EmptyState
      icon={DatabaseZap}
      className="py-12"
      title={t.overview.emptyDatabaseTitle}
      description={t.overview.emptyDatabaseBody}
      action={
        <div className="mt-2 flex flex-col items-center gap-2">
          <div className="border-border bg-muted flex items-center gap-2 rounded border px-2 py-1.5">
            <code className="text-foreground font-mono text-sm select-all">{DEMO_COMMAND}</code>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              onClick={copy}
              aria-label={t.common.copy}
            >
              <Copy aria-hidden />
            </Button>
          </div>
          <p className="text-muted-foreground max-w-sm text-xs">{t.overview.emptyDatabaseHint}</p>
        </div>
      }
    />
  );
}
