import { isSafeNumber, parse } from "lossless-json";
import type { Int, Session } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
  ) {
    super(message);
  }
}
export function parseExact(text: string): unknown {
  return parse(text, undefined, {
    parseNumber: (value) => (isSafeNumber(value) ? Number(value) : value),
  });
}
export async function request<T>(
  path: string,
  session: Session | null,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  const deadline = AbortSignal.timeout(20_000);
  const signal = options.signal
    ? AbortSignal.any([options.signal, deadline])
    : deadline;
  try {
    response = await fetch(path, {
      ...options,
      signal,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(session
          ? {
              Authorization: `Bearer ${session.access_token}`,
              "X-Tenant-ID": session.principal.tenant_id,
            }
          : {}),
        ...options.headers,
      },
      cache: "no-store",
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    if (deadline.aborted)
      throw new ApiError(
        0,
        "request_timeout",
        "The service did not respond within 20 seconds. Refresh to verify the latest state.",
      );
    throw new ApiError(
      0,
      "network_unavailable",
      "The service could not be reached. Check your connection, then refresh.",
    );
  }
  let data: unknown;
  try {
    data = parseExact(await response.text());
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      response.status,
      "invalid_response",
      "The service returned an unreadable response. Refresh to verify the latest state.",
      response.headers.get("X-Request-ID") ?? undefined,
    );
  }
  if (!response.ok) {
    const problem =
      data !== null && typeof data === "object" && !Array.isArray(data)
        ? (data as Record<string, unknown>)
        : {};
    throw new ApiError(
      response.status,
      typeof problem.code === "string" ? problem.code : "request_failed",
      typeof problem.message === "string"
        ? problem.message
        : `The request failed (${response.status}).`,
      typeof problem.request_id === "string"
        ? problem.request_id
        : (response.headers.get("X-Request-ID") ?? undefined),
    );
  }
  return data as T;
}
export const number = (v: Int | null | undefined) =>
  v == null ? "—" : new Intl.NumberFormat("en-US").format(BigInt(v));
export function money(value: Int | null | undefined, currency = "USD"): string {
  if (value == null) return "—";
  try {
    const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency });
    const digits = fmt.resolvedOptions().maximumFractionDigits ?? 2;
    const scale = 10n ** BigInt(digits),
      n = BigInt(value),
      abs = n < 0n ? -n : n,
      whole = abs / scale;
    const parts = fmt.formatToParts(n < 0n ? -(whole || 1n) : whole);
    return parts
      .map((p) =>
        p.type === "fraction"
          ? (abs % scale).toString().padStart(digits, "0")
          : p.type === "integer" && whole === 0n
            ? "0"
            : p.value,
      )
      .join("");
  } catch {
    return `${value} minor units (${currency})`;
  }
}
export function time(value: string | null | undefined) {
  if (!value) return "Not yet recorded";
  const d = new Date(value);
  return Number.isNaN(d.valueOf())
    ? value
    : d.toLocaleString("en-GB", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "UTC",
        hour12: false,
      }) + " UTC";
}
export const title = (v: string) =>
  v.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
export function json(value: unknown) {
  return JSON.stringify(value, null, 2);
}
