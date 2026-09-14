import { DATA_MODE_HEADER, REQUEST_ID_HEADER } from "@/lib/constants";

/**
 * The single HTTP boundary between the browser and the AutoTwin API.
 *
 * Everything that talks to the backend goes through `apiFetch`. That is what makes three
 * cross-cutting concerns possible in exactly one place instead of scattered through components:
 *
 *  - the `X-AutoTwin-Data-Mode` header is lifted out of the response, so any screen can say
 *    “live source unavailable — showing cached data” without the fetch call knowing about it;
 *  - the backend's structured error envelope is turned into a typed `ApiError` carrying the
 *    stable error code and the request id, so an error toast can reference a log line;
 *  - requests are routed through the Next.js rewrite at `/api/backend/*`, which keeps the
 *    API origin out of the client bundle and removes CORS from the development loop.
 */

export type DataMode = "live" | "cache" | "fixture";

export interface ApiResult<T> {
  data: T;
  dataMode: DataMode | null;
  requestId: string | null;
}

interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    details?: Record<string, unknown>;
    request_id?: string;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {},
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }

  /** True when the backend told us a provider is down, rather than that we asked wrongly. */
  get isProviderFailure(): boolean {
    return (
      this.code === "provider_unavailable" ||
      this.code === "provider_timeout" ||
      this.code === "rate_limited"
    );
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/**
 * Browser calls go to the Next.js rewrite; server-side calls (React Server Components, route
 * handlers) must use an absolute origin because there is no relative base URL on the server.
 */
function resolveBase(): string {
  if (typeof window !== "undefined") return "/api/backend";
  const origin = process.env.AUTOTWIN_API_URL ?? "http://localhost:8000";
  return `${origin}/api/v1`;
}

/** Base for the unversioned operational endpoints: /health, /ready, /metrics. */
function resolveRootBase(): string {
  if (typeof window !== "undefined") return "/api/backend-root";
  return process.env.AUTOTWIN_API_URL ?? "http://localhost:8000";
}

export type QueryValue = string | number | boolean | null | undefined | (string | number)[];

export function buildQuery(params: Record<string, QueryValue> = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      search.set(key, value.join(","));
      continue;
    }
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

function isDataMode(value: string | null): value is DataMode {
  return value === "live" || value === "cache" || value === "fixture";
}

export interface ApiFetchOptions extends Omit<RequestInit, "body"> {
  /** Request body; serialised as JSON. */
  json?: unknown;
  /** Abort the request after this many milliseconds. */
  timeoutMs?: number;
}

export interface ApiFetchInternalOptions extends ApiFetchOptions {
  /** Target the unversioned operational endpoints (/health, /ready, /metrics). */
  root?: boolean;
}

export async function apiFetchWithMeta<T>(
  path: string,
  { json, timeoutMs = 30_000, headers, root = false, ...init }: ApiFetchInternalOptions = {},
): Promise<ApiResult<T>> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  // Respect a caller-supplied signal as well as our timeout.
  init.signal?.addEventListener("abort", () => controller.abort(), { once: true });

  let response: Response;
  try {
    response = await fetch(`${root ? resolveRootBase() : resolveBase()}${path}`, {
      ...init,
      method: init.method ?? (json !== undefined ? "POST" : "GET"),
      headers: {
        accept: "application/json",
        ...(json !== undefined ? { "content-type": "application/json" } : {}),
        ...headers,
      },
      body: json !== undefined ? JSON.stringify(json) : undefined,
      signal: controller.signal,
    });
  } catch (cause) {
    if (controller.signal.aborted) {
      throw new ApiError(408, "request_timeout", "The request to the AutoTwin API timed out.");
    }
    throw new ApiError(
      0,
      "network_error",
      cause instanceof Error ? cause.message : "The AutoTwin API could not be reached.",
    );
  } finally {
    clearTimeout(timeout);
  }

  const requestId = response.headers.get(REQUEST_ID_HEADER);
  const dataModeHeader = response.headers.get(DATA_MODE_HEADER);
  const dataMode = isDataMode(dataModeHeader) ? dataModeHeader : null;

  if (!response.ok) {
    let envelope: ErrorEnvelope = {};
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      // A non-JSON error body (a proxy error page, say) is still an error — just a less
      // informative one. Fall through to the status-derived message below.
    }
    throw new ApiError(
      response.status,
      envelope.error?.code ?? `http_${response.status}`,
      envelope.error?.message ?? `${response.status} ${response.statusText}`,
      envelope.error?.details ?? {},
      envelope.error?.request_id ?? requestId,
    );
  }

  if (response.status === 204) {
    return { data: undefined as T, dataMode, requestId };
  }

  return { data: (await response.json()) as T, dataMode, requestId };
}

/** The common case: the payload, without the transport metadata. */
export async function apiFetch<T>(path: string, options?: ApiFetchInternalOptions): Promise<T> {
  const { data } = await apiFetchWithMeta<T>(path, options);
  return data;
}

/** Operational endpoints: `apiFetchRoot("/ready")`. */
export async function apiFetchRoot<T>(path: string, options?: ApiFetchOptions): Promise<T> {
  return apiFetch<T>(path, { ...options, root: true });
}
