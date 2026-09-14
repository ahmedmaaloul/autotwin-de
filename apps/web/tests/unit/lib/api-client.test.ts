import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError, apiFetch, apiFetchRoot, apiFetchWithMeta, buildQuery } from "@/lib/api/client";
import { DATA_MODE_HEADER, REQUEST_ID_HEADER } from "@/lib/constants";

/**
 * The single HTTP boundary.
 *
 * Everything the browser knows about the backend passes through this module, so a defect here
 * is invisible everywhere and fatal everywhere: a dropped filter widens a query without
 * anyone noticing, and an error envelope that fails to parse turns a "DWD unreachable" message
 * into an unhandled `SyntaxError`.
 *
 * No network: `fetch` is stubbed for every test and the assertions are about the request that
 * would have gone out and the response object that comes back.
 */

let fetchMock: Mock;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** A successful JSON response, with whatever headers the test cares about. */
function jsonResponse(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    statusText: "OK",
    headers: { "content-type": "application/json", ...headers },
  });
}

/** The backend's structured error envelope (BUILD_SPEC §7). */
function errorResponse(
  status: number,
  statusText: string,
  envelope: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(envelope), {
    status,
    statusText,
    headers: { "content-type": "application/json", ...headers },
  });
}

/** The URL `apiFetch` actually asked for. */
function requestedUrl(): string {
  return fetchMock.mock.calls[0]?.[0] as string;
}

/** The init object `apiFetch` actually passed. */
function requestInit(): RequestInit {
  return fetchMock.mock.calls[0]?.[1] as RequestInit;
}

describe("buildQuery", () => {
  it.each<[string, Record<string, string | number | boolean | null | undefined | (string | number)[]>, string]>([
    ["no params at all", {}, ""],
    // Not "?" — a bare question mark is a different URL to a cache and to some proxies.
    ["only empty values", { a: null, b: undefined, c: "" }, ""],
    ["plain scalars", { page: 1, page_size: 50 }, "?page=1&page_size=50"],
    // 0 and false are meaningful filter bounds. Dropping them silently widens the query.
    ["zero and false", { min_power_kw: 0, fast_only: false }, "?min_power_kw=0&fast_only=false"],
    // Arrays become one comma-joined value; the comma itself is percent-encoded.
    ["a string array", { bundesland: ["BW", "BY"] }, "?bundesland=BW%2CBY"],
    ["a numeric array", { years: [2023, 2024] }, "?years=2023%2C2024"],
    // An empty array means "no filter", not "match nothing".
    ["an empty array", { bundesland: [] }, ""],
    ["mixed present and absent", { q: "Kassel", operator: null, page: 2 }, "?q=Kassel&page=2"],
  ])("returns %s as %s", (_label, params, expected) => {
    expect(buildQuery(params)).toBe(expected);
  });

  it("percent-encodes values so a German search term survives the wire", () => {
    // "Straße & Co" contains a character outside ASCII and one that would otherwise start a
    // new parameter. Both must come back byte-identical on the server.
    const query = buildQuery({ q: "Straße & Co" });
    expect(query).toBe("?q=Stra%C3%9Fe+%26+Co");
    expect(new URLSearchParams(query).get("q")).toBe("Straße & Co");
  });

  it("round-trips an array through the encoder", () => {
    // The server splits on the comma, so the decoded value must be exactly "BW,BY,HE".
    const query = buildQuery({ bundesland: ["BW", "BY", "HE"] });
    expect(new URLSearchParams(query).get("bundesland")).toBe("BW,BY,HE");
  });

  it("encodes the key as well as the value", () => {
    expect(buildQuery({ "odd key": 1 })).toBe("?odd+key=1");
  });

  it("preserves the order the caller wrote", () => {
    // Stable query strings are cache keys — for the browser, for React Query, and for a proxy.
    expect(buildQuery({ b: 1, a: 2 })).toBe("?b=1&a=2");
  });

  it("is safe to call with no argument at all", () => {
    expect(buildQuery()).toBe("");
  });
});

describe("apiFetch — the happy path", () => {
  it("routes browser requests through the Next.js rewrite", async () => {
    // Keeping the API origin out of the client bundle is the whole reason the rewrite exists;
    // an absolute URL here would also reintroduce CORS.
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }));
    await apiFetch("/charging/stations?page=1");
    expect(requestedUrl()).toBe("/api/backend/charging/stations?page=1");
  });

  it("routes operational endpoints to the unversioned base", async () => {
    // /health, /ready and /metrics live outside /api/v1 and need their own rewrite.
    fetchMock.mockResolvedValue(jsonResponse({ status: "ok" }));
    await apiFetchRoot("/ready");
    expect(requestedUrl()).toBe("/api/backend-root/ready");
  });

  it("returns the parsed body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ total: 116_440 }));
    await expect(apiFetch<{ total: number }>("/charging/stations")).resolves.toEqual({
      total: 116_440,
    });
  });

  it("sends GET with no content-type when there is no body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/dashboard/summary");
    const init = requestInit();
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(new Headers(init.headers as HeadersInit).get("accept")).toBe("application/json");
    expect(init.headers).not.toHaveProperty("content-type");
  });

  it("switches to POST and serialises the body when `json` is supplied", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await apiFetch("/routes/analyze", { json: { slug: "frankfurt-stuttgart", soc: 80 } });
    const init = requestInit();
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers as HeadersInit).get("content-type")).toBe(
      "application/json",
    );
    expect(init.body).toBe('{"slug":"frankfurt-stuttgart","soc":80}');
  });

  it("lets an explicit method win over the inferred one", async () => {
    // A PATCH with a body must stay a PATCH, or a partial update becomes a create.
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/simulation/runs/1", { method: "PATCH", json: { state: "paused" } });
    expect(requestInit().method).toBe("PATCH");
  });

  it("returns undefined for 204 without trying to parse a body", async () => {
    // A 204 has no body at all; calling .json() on it would reject.
    fetchMock.mockResolvedValue(new Response(null, { status: 204, statusText: "No Content" }));
    await expect(apiFetch("/simulation/stop", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("apiFetchWithMeta — transport metadata", () => {
  it.each<[string | null, "live" | "cache" | "fixture" | null]>([
    ["live", "live"],
    ["cache", "cache"],
    ["fixture", "fixture"],
    // An unrecognised value must not be forwarded: the UI switches on this to decide whether
    // to show the "live source unavailable" banner, and an unknown mode is not a known mode.
    ["stale", null],
    ["LIVE", null],
    ["", null],
    [null, null],
  ])("lifts a %s data-mode header as %s", async (headerValue, expected) => {
    fetchMock.mockResolvedValue(
      jsonResponse({}, headerValue === null ? {} : { [DATA_MODE_HEADER]: headerValue }),
    );
    const result = await apiFetchWithMeta("/charging/stations");
    expect(result.dataMode).toBe(expected);
  });

  it("lifts the request id so an error toast can quote a log line", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, { [REQUEST_ID_HEADER]: "01JG7X" }));
    const result = await apiFetchWithMeta("/charging/stations");
    expect(result.requestId).toBe("01JG7X");
  });

  it("reports null metadata when the backend sends neither header", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [] }));
    const result = await apiFetchWithMeta<{ items: unknown[] }>("/charging/stations");
    expect(result).toEqual({ data: { items: [] }, dataMode: null, requestId: null });
  });

  it("carries metadata on a 204 as well", async () => {
    // A mutation that answers 204 can still have been served in degraded mode.
    fetchMock.mockResolvedValue(
      new Response(null, { status: 204, headers: { [DATA_MODE_HEADER]: "fixture" } }),
    );
    const result = await apiFetchWithMeta("/simulation/stop", { method: "POST" });
    expect(result.dataMode).toBe("fixture");
    expect(result.data).toBeUndefined();
  });
});

describe("apiFetch — error handling", () => {
  it("parses the error envelope into a typed ApiError", async () => {
    fetchMock.mockResolvedValue(
      errorResponse(503, "Service Unavailable", {
        error: {
          code: "provider_unavailable",
          message: "DWD open data unreachable",
          details: { source: "dwd" },
          request_id: "01JG7X",
        },
      }),
    );

    const error = await apiFetch("/weather/current").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(503);
    expect(apiError.code).toBe("provider_unavailable");
    expect(apiError.message).toBe("DWD open data unreachable");
    expect(apiError.details).toEqual({ source: "dwd" });
    expect(apiError.requestId).toBe("01JG7X");
    expect(apiError.name).toBe("ApiError");
  });

  it("falls back to the response header when the envelope omits the request id", async () => {
    fetchMock.mockResolvedValue(
      errorResponse(
        404,
        "Not Found",
        { error: { code: "not_found", message: "No such station" } },
        { [REQUEST_ID_HEADER]: "from-header" },
      ),
    );
    const error = (await apiFetch("/charging/stations/zzz").catch((c: unknown) => c)) as ApiError;
    expect(error.requestId).toBe("from-header");
    expect(error.isNotFound).toBe(true);
  });

  it("derives code and message from the status when the JSON is not an envelope", async () => {
    // A FastAPI default handler answers {"detail": ...}; we still need a stable code.
    fetchMock.mockResolvedValue(
      errorResponse(500, "Internal Server Error", { detail: "boom" }),
    );
    const error = (await apiFetch("/dashboard/summary").catch((c: unknown) => c)) as ApiError;
    expect(error.code).toBe("http_500");
    expect(error.message).toBe("500 Internal Server Error");
    expect(error.details).toEqual({});
    expect(error.requestId).toBeNull();
  });

  it("survives a non-JSON error body instead of throwing SyntaxError", async () => {
    // An nginx or Next.js proxy error page is HTML. Letting `response.json()` reject would
    // replace a readable "502 Bad Gateway" with an unhandled parser error in the console.
    fetchMock.mockResolvedValue(
      new Response("<html><body>502 Bad Gateway</body></html>", {
        status: 502,
        statusText: "Bad Gateway",
        headers: { "content-type": "text/html" },
      }),
    );

    const error = await apiFetch("/charging/stations").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(SyntaxError);
    expect((error as ApiError).code).toBe("http_502");
    expect((error as ApiError).message).toBe("502 Bad Gateway");
  });

  it("survives a completely empty error body", async () => {
    fetchMock.mockResolvedValue(new Response("", { status: 500, statusText: "Internal Server Error" }));
    const error = (await apiFetch("/x").catch((c: unknown) => c)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe("http_500");
  });

  it("reports an unreachable backend as status 0 / network_error", async () => {
    // Status 0 is the signal `ErrorState` uses to show "Backend nicht erreichbar" instead of a
    // server-side error message the user cannot act on.
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const error = (await apiFetch("/dashboard/summary").catch((c: unknown) => c)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.code).toBe("network_error");
    expect(error.message).toBe("Failed to fetch");
  });

  it("uses a readable message when the rejection is not an Error", async () => {
    fetchMock.mockRejectedValue("something odd");
    const error = (await apiFetch("/x").catch((c: unknown) => c)) as ApiError;
    expect(error.status).toBe(0);
    expect(error.message).toBe("The AutoTwin API could not be reached.");
  });

  it("turns its own timeout into a 408 request_timeout", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );

    const pending = apiFetch("/charging/stations", { timeoutMs: 5_000 });
    const assertion = expect(pending).rejects.toMatchObject({
      status: 408,
      code: "request_timeout",
    });
    await vi.advanceTimersByTimeAsync(5_000);
    await assertion;
  });

  it("does not fire the timeout for a request that answers in time", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }));
    await expect(apiFetch("/x", { timeoutMs: 5_000 })).resolves.toEqual({ ok: true });
    // If the timer were left pending, advancing past it would abort an already-settled request.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("surfaces a caller-side abort as a typed error, never as a raw DOMException", async () => {
    // React Query aborts in-flight queries on unmount. Whatever the callers do with it, the
    // rejection that escapes this module is always an ApiError.
    const outer = new AbortController();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );

    const pending = apiFetch("/charging/stations", { signal: outer.signal });
    outer.abort();

    const error = await pending.catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(408);
  });
});

describe("ApiError.isProviderFailure", () => {
  it.each<[string, boolean]>([
    // True: the upstream source is down. ADR 005's fallback chain is the right response.
    ["provider_unavailable", true],
    ["provider_timeout", true],
    ["rate_limited", true],
    // False: we asked wrongly, or our own deployment is misconfigured. Retrying a different
    // source would not help.
    ["validation_error", false],
    ["not_found", false],
    ["configuration_missing", false],
    ["network_error", false],
    ["request_timeout", false],
    ["http_500", false],
    ["", false],
  ])("is %s for code %s", (code, expected) => {
    expect(new ApiError(503, code, "x").isProviderFailure).toBe(expected);
  });

  /** REGRESSION. A 502 means the upstream answered with something unparseable — their
   * failure, not the caller's, so the UI must not tell the reader to check their request. */
  it("classifies invalid_source_data as a provider failure (BUILD_SPEC §7)", () => {
    expect(new ApiError(502, "invalid_source_data", "unparseable CSV").isProviderFailure).toBe(true);
  });
});

describe("ApiError shape", () => {
  it("defaults details and requestId rather than leaving them undefined", () => {
    const error = new ApiError(500, "http_500", "boom");
    expect(error.details).toEqual({});
    expect(error.requestId).toBeNull();
  });

  it.each<[number, boolean]>([
    [404, true],
    [400, false],
    [500, false],
    [0, false],
  ])("isNotFound is %s for status %s", (status, expected) => {
    expect(new ApiError(status, "code", "m").isNotFound).toBe(expected);
  });

  it("is catchable as an Error, which is what error boundaries check", () => {
    const error = new ApiError(500, "http_500", "boom");
    expect(error instanceof Error).toBe(true);
    expect(error.stack).toBeTruthy();
  });
});

describe("caller-supplied headers", () => {
  /** The client normalises every `HeadersInit` form into a `Headers`, so read it as one. */
  function sentHeaders(): Headers {
    return new Headers(requestInit().headers as HeadersInit);
  }

  it("merges a plain-object header into the request", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/x", { headers: { "x-trace": "abc" } });
    expect(sentHeaders().get("accept")).toBe("application/json");
    expect(sentHeaders().get("x-trace")).toBe("abc");
  });

  it("lets a caller override the accept header", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/metrics", { root: true, headers: { accept: "text/plain" } });
    expect(sentHeaders().get("accept")).toBe("text/plain");
  });

  /**
   * REGRESSION. `headers` is typed `HeadersInit`, so a `Headers` instance or a `string[][]`
   * compiles. Merging with object spread dropped both silently — a `Headers` has no own
   * enumerable properties — losing an auth or trace header with no compile or runtime error.
   */
  it("keeps a header passed as a Headers instance", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await apiFetch("/x", { headers: new Headers({ "x-trace": "abc" }) });
    expect(sentHeaders().get("x-trace")).toBe("abc");
  });
});
