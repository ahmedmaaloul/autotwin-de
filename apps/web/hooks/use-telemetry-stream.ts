"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { loadSnapshotVehicles, SNAPSHOT_ENABLED } from "@/lib/api/snapshot";
import type { VehicleLive } from "@/types/domain";

export type StreamStatus = "connecting" | "open" | "closed" | "error";

interface TelemetryStreamState {
  vehicles: Map<string, VehicleLive>;
  status: StreamStatus;
  eventsReceived: number;
  lastEventAt: number | null;
}

/**
 * Server-Sent Events subscription for live vehicle telemetry.
 *
 * SSE rather than WebSocket because the traffic is one-directional: the server pushes vehicle
 * positions, the browser never pushes back. SSE brings automatic reconnection, works through
 * any HTTP proxy, and needs no protocol upgrade — a WebSocket would be strictly more machinery
 * for the same result (BUILD_SPEC §40).
 *
 * Two details make this usable with several hundred vehicles:
 *
 *  - **The server sends deltas.** Each message carries only the vehicles whose telemetry
 *    advanced, and they are merged into a `Map` keyed by `vehicle_id`. The full fleet is never
 *    retransmitted.
 *  - **Rendering is decoupled from arrival.** Messages mutate a ref; React state is published
 *    on an animation frame. A burst of 300 events in one tick therefore causes one render, not
 *    three hundred.
 */
export function useTelemetryStream({
  enabled = true,
  bbox,
}: { enabled?: boolean; bbox?: string } = {}) {
  const [state, setState] = useState<TelemetryStreamState>(() => ({
    vehicles: new Map(),
    status: "connecting",
    eventsReceived: 0,
    lastEventAt: null,
  }));

  const bufferRef = useRef(new Map<string, VehicleLive>());
  const frameRef = useRef<number | null>(null);
  const countRef = useRef(0);

  const publish = useCallback(() => {
    frameRef.current = null;
    setState((previous) => ({
      ...previous,
      vehicles: new Map(bufferRef.current),
      eventsReceived: countRef.current,
      lastEventAt: Date.now(),
    }));
  }, []);

  const schedulePublish = useCallback(() => {
    if (frameRef.current != null) return;
    frameRef.current = window.requestAnimationFrame(publish);
  }, [publish]);

  useEffect(() => {
    if (!enabled) return;

    // Static demo: there is no stream to subscribe to. Load the captured fleet once and report
    // the connection as open; the positions are a snapshot and the site banner says so.
    if (SNAPSHOT_ENABLED) {
      let cancelled = false;
      void loadSnapshotVehicles<VehicleLive>().then((fleet) => {
        if (cancelled) return;
        for (const vehicle of fleet) bufferRef.current.set(vehicle.vehicle_id, vehicle);
        countRef.current += fleet.length;
        setState((previous) => ({ ...previous, status: "open" }));
        schedulePublish();
      });
      return () => {
        cancelled = true;
      };
    }

    const params = new URLSearchParams();
    if (bbox) params.set("bbox", bbox);
    const query = params.toString();
    const source = new EventSource(`/api/backend/stream/telemetry${query ? `?${query}` : ""}`);

    source.onopen = () => setState((previous) => ({ ...previous, status: "open" }));

    const onTelemetry = (event: MessageEvent<string>) => {
      let payload: VehicleLive[];
      try {
        payload = JSON.parse(event.data) as VehicleLive[];
      } catch {
        // A malformed frame is a server bug, not a reason to tear down a working stream.
        return;
      }
      for (const vehicle of payload) {
        bufferRef.current.set(vehicle.vehicle_id, vehicle);
      }
      countRef.current += payload.length;
      schedulePublish();
    };

    source.addEventListener("telemetry", onTelemetry as EventListener);

    source.onerror = () => {
      // EventSource reconnects on its own; surface the interruption without closing it, so the
      // UI can say "reconnecting" rather than pretending the data is current.
      setState((previous) => ({
        ...previous,
        status: source.readyState === EventSource.CLOSED ? "closed" : "error",
      }));
    };

    return () => {
      source.removeEventListener("telemetry", onTelemetry as EventListener);
      source.close();
      if (frameRef.current != null) window.cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [enabled, bbox, schedulePublish]);

  const vehicles = useMemo(() => [...state.vehicles.values()], [state.vehicles]);

  // Derived rather than stored: when the caller disables the stream the status is closed by
  // definition, and writing that into state during an effect would be a cascading render.
  const status: StreamStatus = enabled ? state.status : "closed";

  return {
    vehicles,
    vehiclesById: state.vehicles,
    status,
    eventsReceived: state.eventsReceived,
    lastEventAt: state.lastEventAt,
  };
}
