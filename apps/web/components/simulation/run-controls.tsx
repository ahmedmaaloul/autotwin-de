"use client";

import { Pause, Play, RotateCcw, Square } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { StatusBadge } from "@/components/shared/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useCreateSimulation, useSimulationControl, useVehicleProfiles } from "@/hooks/use-autotwin";
import { ApiError } from "@/lib/api/client";
import { formatDateTime } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { SimulationRun, SimulationState } from "@/types/domain";

import { toCreateRequest, type SimulationConfig } from "./config";
import { simulationLabel, simulationTone } from "./state-labels";

/**
 * The lifecycle of a run, as five buttons.
 *
 * Each button is enabled exactly for the states in which its transition is legal, so the
 * control surface teaches the state machine instead of failing at the API. `Starten` doubles as
 * "create a run from the form and start it" when there is nothing to resume — the operator's
 * intent is the same, and making them press two buttons to express it would be ceremony.
 */
export function RunControls({
  config,
  state,
  activeRun,
  activeRunId,
  onRunCreated,
  className,
}: {
  config: SimulationConfig;
  state: SimulationState | null;
  activeRun: SimulationRun | null;
  activeRunId: string | null;
  onRunCreated: (runId: string) => void;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const control = useSimulationControl();
  const create = useCreateSimulation();
  const profiles = useVehicleProfiles();

  const busy = control.isPending || create.isPending;
  const canStartExisting = Boolean(activeRunId) && state === "pending";

  const dispatch = (action: "start" | "pause" | "resume" | "stop" | "reset") => {
    if (!activeRunId) return;
    control.mutate({ id: activeRunId, action });
  };

  const startRun = () => {
    if (canStartExisting) {
      dispatch("start");
      return;
    }
    const name = `${t.simulation.runNamePrefix} ${formatDateTime(new Date(), locale, {
      dateStyle: "short",
      timeStyle: "short",
    })}`;
    const request = toCreateRequest(config, {
      name,
      vehicleCodes: (profiles.data ?? []).map((profile) => profile.code),
    });
    create.mutate(request, {
      onSuccess: (run) => {
        onRunCreated(run.id);
        control.mutate({ id: run.id, action: "start" });
      },
    });
  };

  const actions: {
    key: string;
    label: string;
    icon: LucideIcon;
    enabled: boolean;
    onClick: () => void;
    variant?: "default" | "outline" | "destructive";
  }[] = [
    {
      key: "start",
      label: canStartExisting ? t.simulation.start : t.simulation.createAndStart,
      icon: Play,
      enabled: canStartExisting || state == null || isFinished(state),
      onClick: startRun,
      variant: "default",
    },
    {
      key: "pause",
      label: t.simulation.pause,
      icon: Pause,
      enabled: state === "running",
      onClick: () => dispatch("pause"),
    },
    {
      key: "resume",
      label: t.simulation.resume,
      icon: Play,
      enabled: state === "paused",
      onClick: () => dispatch("resume"),
    },
    {
      key: "stop",
      label: t.simulation.stop,
      icon: Square,
      enabled: state === "running" || state === "paused",
      onClick: () => dispatch("stop"),
      variant: "destructive",
    },
    {
      key: "reset",
      label: t.simulation.reset,
      icon: RotateCcw,
      enabled: state === "paused" || (state != null && isFinished(state)),
      onClick: () => dispatch("reset"),
    },
  ];

  const error = control.error ?? create.error;

  return (
    <div className={cn("space-y-3", className)}>
      <div className="flex flex-wrap items-center gap-2">
        {actions.map((action) => (
          <Button
            key={action.key}
            type="button"
            size="sm"
            variant={action.variant ?? "outline"}
            disabled={!action.enabled || busy}
            onClick={action.onClick}
          >
            <action.icon aria-hidden />
            {create.isPending && action.key === "start" ? t.simulation.creating : action.label}
          </Button>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">{t.simulation.activeRun}</span>
        {activeRunId ? (
          <>
            <span className="text-foreground truncate font-mono text-xs">
              {activeRun?.name ?? activeRunId}
            </span>
            {state ? (
              <StatusBadge
                status={simulationTone(state)}
                label={simulationLabel(state, t)}
                pulse={state === "running"}
              />
            ) : null}
          </>
        ) : (
          <span className="text-muted-foreground text-xs">{t.simulation.noActiveRun}</span>
        )}
      </div>

      {error ? (
        <Alert variant="destructive" className="items-start">
          <AlertTitle>{t.simulation.actionFailed}</AlertTitle>
          <AlertDescription>
            <p>{error instanceof ApiError ? error.message : t.states.errorBody}</p>
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}

function isFinished(state: SimulationState): boolean {
  return state === "stopped" || state === "completed" || state === "failed";
}
