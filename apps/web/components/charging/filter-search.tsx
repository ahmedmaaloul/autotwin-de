"use client";

import { Search, X } from "lucide-react";
import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTranslations } from "@/providers/locale-provider";

/**
 * Free-text search over station name, operator and place.
 *
 * Submitted rather than debounced: the query hits a 90 000-row table, and firing a request per
 * keystroke would make the table flicker through three wrong result sets on the way to the
 * right one. Enter (or the button) commits; the URL then carries the term.
 *
 * The draft re-syncs from the committed term during render, so dismissing the search chip
 * empties the field without an effect writing state behind the reader's back.
 */
export function FilterSearch({
  value,
  onSubmit,
}: {
  value: string;
  onSubmit: (term: string) => void;
}) {
  const t = useTranslations();
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [committed, setCommitted] = useState(value);

  if (committed !== value) {
    setCommitted(value);
    setDraft(value);
  }

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit(draft.trim());
  };

  return (
    <form onSubmit={submit} className="min-w-0 space-y-2" role="search">
      <label htmlFor={id} className="eyebrow block">
        {t.charging.searchLabel}
      </label>
      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <Search
            className="text-muted-foreground pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2"
            aria-hidden
          />
          <Input
            id={id}
            type="search"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t.charging.searchPlaceholder}
            className="h-8 pl-7 text-xs"
          />
          {draft ? (
            <button
              type="button"
              onClick={() => {
                setDraft("");
                onSubmit("");
              }}
              aria-label={`${t.charging.removeFilter}: ${t.charging.searchLabel}`}
              className="hover:bg-muted focus-visible:ring-ring absolute top-1/2 right-1.5 inline-flex size-5 -translate-y-1/2 items-center justify-center rounded-sm focus-visible:ring-2 focus-visible:outline-none"
            >
              <X className="size-3" aria-hidden />
            </button>
          ) : null}
        </div>
        <Button type="submit" variant="outline" size="sm" className="h-8 px-2.5 text-xs">
          {t.common.search}
        </Button>
      </div>
    </form>
  );
}
