/**
 * Transport-level types shared by the live client and the static snapshot resolver.
 *
 * They live in their own module so `client.ts` and `snapshot.ts` can both import them without
 * importing each other's runtime code.
 */

export type DataMode = "live" | "cache" | "fixture";

export interface ApiResult<T> {
  data: T;
  dataMode: DataMode | null;
  requestId: string | null;
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

  /**
   * True when the backend told us an upstream source failed, rather than that we asked wrongly.
   *
   * `invalid_source_data` (502) belongs here: it means the source answered with something
   * unparseable, which is a failure on their side. Treating it as a client error would show the
   * reader "check your request" when the right message is "the source is having a bad day".
   */
  get isProviderFailure(): boolean {
    return (
      this.code === "provider_unavailable" ||
      this.code === "provider_timeout" ||
      this.code === "invalid_source_data" ||
      this.code === "rate_limited"
    );
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

export function isDataMode(value: string | null | undefined): value is DataMode {
  return value === "live" || value === "cache" || value === "fixture";
}
