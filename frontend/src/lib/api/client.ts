// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The typed API client.
 *
 * `api` is an openapi-fetch client over the generated `paths` (npm run
 * gen:api). Paths in the schema already carry the `/api/v1` prefix, so the
 * base URL is the page's own origin. Every request:
 *
 * - is same-origin with `credentials: "same-origin"` (the session cookie is
 *   `__Host-`, `Secure`, `SameSite=Lax`; nothing is ever cross-origin);
 * - on `POST`/`PUT`/`PATCH`/`DELETE`, carries `X-CSRF-Token` from the token
 *   store fed by `GET /api/v1/auth/session` (lib/csrf.ts);
 * - on error, is turned into an {@link ApiError} parsed from the RFC 9457
 *   problem-details body (docs/API.md) by {@link unwrap}.
 *
 * Usage: `const accounts = await unwrap(api.GET("/api/v1/accounts"));`
 */
import createClient, { type Client, type Middleware } from "openapi-fetch";

import { CSRF_HEADER, csrfStore, type CsrfTokenStore } from "../csrf";
import type { paths } from "./schema";

export const PROBLEM_TYPE_PREFIX = "urn:perfin:problem:";
export const PROBLEM_MEDIA_TYPE = "application/problem+json";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const MAX_TEXT_LENGTH = 500;
const RESERVED = new Set(["type", "title", "status", "detail", "instance"]);

/** One entry of a validation problem's `errors` extension (backend `validation_errors`). */
export interface ValidationIssue {
  readonly loc: readonly (string | number)[];
  readonly msg: string;
  readonly type: string;
}

/** A failed API call, parsed from problem details. Never holds a stack from the server. */
export class ApiError extends Error {
  readonly status: number;
  /** The problem `type` URI, e.g. `urn:perfin:problem:account-not-found`. */
  readonly type: string;
  readonly title: string;
  readonly detail: string | null;
  readonly instance: string | null;
  /** Extension members other than `errors` (e.g. `request_id`). */
  readonly extensions: Readonly<Record<string, unknown>>;
  readonly issues: readonly ValidationIssue[];

  constructor(init: {
    status: number;
    type: string;
    title: string;
    detail?: string | null;
    instance?: string | null;
    extensions?: Record<string, unknown>;
    issues?: readonly ValidationIssue[];
  }) {
    super(init.detail ?? init.title);
    this.name = "ApiError";
    this.status = init.status;
    this.type = init.type;
    this.title = init.title;
    this.detail = init.detail ?? null;
    this.instance = init.instance ?? null;
    this.extensions = init.extensions ?? {};
    this.issues = init.issues ?? [];
  }

  /** The slug after `urn:perfin:problem:`, or `null` for a foreign type. */
  get slug(): string | null {
    return this.type.startsWith(PROBLEM_TYPE_PREFIX) ? this.type.slice(PROBLEM_TYPE_PREFIX.length) : null;
  }

  get isNetworkError(): boolean {
    return this.status === 0;
  }

  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  /** Validation messages keyed by dotted field path (without the leading "body"). */
  fieldErrors(): Record<string, string> {
    const result = new Map<string, string>();
    for (const issue of this.issues) {
      const path = issue.loc[0] === "body" || issue.loc[0] === "query" ? issue.loc.slice(1) : issue.loc;
      const key = path.join(".");
      if (!result.has(key)) {
        result.set(key, issue.msg);
      }
    }
    return Object.fromEntries(result);
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function clip(text: string): string {
  return text.length > MAX_TEXT_LENGTH ? `${text.slice(0, MAX_TEXT_LENGTH)}…` : text;
}

function statusSlug(status: number): string {
  if (status === 0) {
    return "network-error";
  }
  return status >= 500 ? "server-error" : `http-${String(status)}`;
}

function defaultTitle(status: number, statusText: string): string {
  if (status === 0) {
    return "Network error";
  }
  return statusText.trim() !== "" ? clip(statusText) : `Request failed (${String(status)})`;
}

function parseIssues(value: unknown): ValidationIssue[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const issues: ValidationIssue[] = [];
  for (const item of value.slice(0, 100)) {
    if (!isRecord(item) || !Array.isArray(item["loc"]) || typeof item["msg"] !== "string") {
      continue;
    }
    const loc = item["loc"].filter((part): part is string | number => typeof part === "string" || typeof part === "number");
    issues.push({ loc, msg: clip(item["msg"]), type: typeof item["type"] === "string" ? item["type"] : "value_error" });
  }
  return issues;
}

/**
 * Build an {@link ApiError} from a status and an already-decoded body (an
 * object for JSON, a string for text, or nothing). Malformed or hostile
 * bodies degrade to a generic error for the status.
 */
export function problemFromBody(status: number, body: unknown, statusText = ""): ApiError {
  if (!isRecord(body)) {
    return new ApiError({
      status,
      type: `${PROBLEM_TYPE_PREFIX}${statusSlug(status)}`,
      title: defaultTitle(status, statusText),
    });
  }
  const type = typeof body["type"] === "string" && body["type"].length > 0 ? clip(body["type"]) : "about:blank";
  const title = typeof body["title"] === "string" && body["title"].length > 0 ? clip(body["title"]) : defaultTitle(status, statusText);
  const detail = typeof body["detail"] === "string" ? clip(body["detail"]) : null;
  const instance = typeof body["instance"] === "string" ? clip(body["instance"]) : null;
  // The HTTP status is authoritative; a body `status` that disagrees is ignored.
  const extensions: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(body)) {
    if (!RESERVED.has(key) && key !== "errors" && key !== "__proto__") {
      // eslint-disable-next-line security/detect-object-injection -- plain own-property copy into a fresh object
      extensions[key] = value;
    }
  }
  return new ApiError({ status, type, title, detail, instance, extensions, issues: parseIssues(body["errors"]) });
}

/** Read a `Response` and turn it into an {@link ApiError}. Safe on any body. */
export async function parseApiError(response: Response): Promise<ApiError> {
  const contentType = response.headers.get("content-type") ?? "";
  let body: unknown = null;
  if (contentType.includes("json")) {
    try {
      body = (await response.json()) as unknown;
    } catch {
      body = null;
    }
  }
  return problemFromBody(response.status, body, response.statusText);
}

export function networkError(cause: unknown): ApiError {
  const error = new ApiError({
    status: 0,
    type: `${PROBLEM_TYPE_PREFIX}network-error`,
    title: "Network error",
    detail: "The server could not be reached.",
  });
  return Object.assign(error, { cause });
}

/** Adds the CSRF header to unsafe methods and drops a token the server rejected. */
export function csrfMiddleware(store: CsrfTokenStore): Middleware {
  return {
    async onRequest({ request }) {
      if (!UNSAFE_METHODS.has(request.method.toUpperCase())) {
        return undefined;
      }
      const token = await store.ensure();
      if (typeof token === "string") {
        request.headers.set(CSRF_HEADER, token);
      }
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) {
        store.clear();
      } else if (response.status === 403 && (response.headers.get("content-type") ?? "").includes("json")) {
        // Peek at a copy: a csrf-failed problem means the token is stale.
        void response
          .clone()
          .json()
          .then((body: unknown) => {
            if (isRecord(body) && body["type"] === `${PROBLEM_TYPE_PREFIX}csrf-failed`) {
              store.clear();
            }
          })
          .catch(() => undefined);
      }
      return undefined;
    },
  };
}

export type ApiClient = Client<paths>;

export interface ApiClientOptions {
  baseUrl?: string;
  fetch?: (request: Request) => Promise<Response>;
  csrf?: CsrfTokenStore;
}

function defaultBaseUrl(): string {
  return typeof window === "undefined" ? "" : window.location.origin;
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? defaultBaseUrl(),
    credentials: "same-origin",
    cache: "no-store",
    headers: { Accept: `application/json, ${PROBLEM_MEDIA_TYPE}` },
    // Resolve fetch per call so tests (and polyfills) can replace it.
    fetch: options.fetch ?? ((request: Request) => globalThis.fetch(request)),
  });
  client.use(csrfMiddleware(options.csrf ?? csrfStore));
  return client;
}

export const api: ApiClient = createApiClient();

interface FetchResult<D> {
  data?: D;
  error?: unknown;
  response: Response;
}

/**
 * Await an openapi-fetch call and return its data, or throw {@link ApiError}.
 * Network failures become an `ApiError` with status 0.
 */
export async function unwrap<D>(call: Promise<FetchResult<D>>): Promise<D> {
  let result: FetchResult<D>;
  try {
    result = await call;
  } catch (cause) {
    throw networkError(cause);
  }
  if (!result.response.ok) {
    throw problemFromBody(result.response.status, result.error, result.response.statusText);
  }
  return result.data as D;
}

/** A short, user-facing message for any thrown value. Never includes stacks. */
export function errorMessage(error: unknown): string {
  if (isApiError(error)) {
    return error.detail ?? error.title;
  }
  return "Something went wrong.";
}
