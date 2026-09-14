import type { StatusTone } from "@/components/shared/status-badge";
import type { Messages } from "@/lib/i18n/messages/de";
import type { SimulationState } from "@/types/domain";

/**
 * `SimulationState` → the shared status vocabulary.
 *
 * Written once and imported by the controls, the status board and the run table, because three
 * surfaces disagreeing about what "stopping" looks like is exactly the kind of drift a design
 * system exists to prevent.
 */
export function simulationTone(state: SimulationState): StatusTone {
  switch (state) {
    case "running":
      return "healthy";
    case "paused":
    case "stopping":
      return "delayed";
    case "failed":
      return "failed";
    case "completed":
    case "stopped":
    case "pending":
      return "neutral";
  }
}

export function simulationLabel(state: SimulationState, t: Messages): string {
  switch (state) {
    case "pending":
      return t.status.pending;
    case "running":
      return t.status.running;
    case "paused":
      return t.status.paused;
    case "stopping":
      return t.simulation.stopping;
    case "stopped":
      return t.status.stopped;
    case "completed":
      return t.status.completed;
    case "failed":
      return t.status.failed;
  }
}
