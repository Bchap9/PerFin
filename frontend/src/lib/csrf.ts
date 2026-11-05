// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * CSRF token store (SECURITY.md A5, AUTH.md "CSRF").
 *
 * The token comes from `GET /api/v1/auth/session` and is sent back in the
 * `X-CSRF-Token` header on every state-changing request. With a session it is
 * bound to the session; without one the server issues a signed pre-auth
 * token (paired with the `__Host-perfin-pre` cookie) for the login endpoints.
 *
 * The token lives in memory only (never in localStorage). A 404, a server
 * error or a network error yields the `unavailable` state and no token, and
 * the app keeps working. A `401 mfa-required` problem (password accepted,
 * second factor outstanding) is the `mfa_pending` state: its token is bound
 * to the pending session and only `POST /auth/mfa/verify` accepts it.
 */

export const CSRF_HEADER = "X-CSRF-Token";
export const SESSION_PATH = "/api/v1/auth/session";
const MAX_TOKEN_LENGTH = 256;
const TOKEN_RE = /^[A-Za-z0-9_\-.~+/=]+$/;
const MFA_REQUIRED_TYPE = "urn:perfin:problem:mfa-required";
const KNOWN_FACTORS = new Set(["totp", "webauthn"]);

export type SessionState =
  | { status: "authenticated"; csrfToken: string | null; data: Readonly<Record<string, unknown>> }
  | { status: "anonymous"; csrfToken: string | null }
  | { status: "mfa_pending"; csrfToken: string | null; methods: readonly string[] }
  | { status: "unavailable"; csrfToken: null; httpStatus: number | null };

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const defaultFetch: FetchLike = (input, init) => globalThis.fetch(input, init);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readToken(body: unknown): string | null {
  if (!isRecord(body)) {
    return null;
  }
  const raw = body["csrf_token"] ?? body["csrfToken"];
  if (typeof raw !== "string" || raw.length === 0 || raw.length > MAX_TOKEN_LENGTH || !TOKEN_RE.test(raw)) {
    return null;
  }
  return raw;
}

async function readJson(response: Response): Promise<unknown> {
  const type = response.headers.get("content-type") ?? "";
  if (!type.includes("json")) {
    return null;
  }
  try {
    return (await response.json()) as unknown;
  } catch {
    return null;
  }
}

function pendingMethods(body: Record<string, unknown>): string[] | null {
  const mfa = body["mfa"];
  const pending = isRecord(mfa) && mfa["pending"] === true;
  if (body["type"] !== MFA_REQUIRED_TYPE && !pending) {
    return null;
  }
  const raw = isRecord(mfa) && Array.isArray(mfa["methods"]) ? mfa["methods"] : [];
  return raw.filter((item): item is string => typeof item === "string" && KNOWN_FACTORS.has(item));
}

/** Interpret a `GET /auth/session` response. Exported for tests. */
export async function sessionFromResponse(response: Response): Promise<SessionState> {
  if (response.status === 401 || response.status === 403) {
    const body = await readJson(response);
    const methods = response.status === 401 && isRecord(body) ? pendingMethods(body) : null;
    if (methods !== null) {
      return { status: "mfa_pending", csrfToken: readToken(body), methods };
    }
    return { status: "anonymous", csrfToken: readToken(body) };
  }
  if (!response.ok) {
    return { status: "unavailable", csrfToken: null, httpStatus: response.status };
  }
  const body = await readJson(response);
  if (!isRecord(body)) {
    return { status: "unavailable", csrfToken: null, httpStatus: response.status };
  }
  const token = readToken(body);
  if (body["authenticated"] === false) {
    return { status: "anonymous", csrfToken: token };
  }
  return { status: "authenticated", csrfToken: token, data: body };
}

export async function fetchSession(fetchImpl: FetchLike = defaultFetch): Promise<SessionState> {
  try {
    const response = await fetchImpl(SESSION_PATH, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    return await sessionFromResponse(response);
  } catch {
    return { status: "unavailable", csrfToken: null, httpStatus: null };
  }
}

export class CsrfTokenStore {
  private token: string | null = null;
  private inflight: Promise<SessionState> | null = null;
  private readonly fetchImpl: FetchLike;
  private readonly listeners = new Set<(state: SessionState) => void>();

  constructor(fetchImpl: FetchLike = defaultFetch) {
    this.fetchImpl = fetchImpl;
  }

  get(): string | null {
    return this.token;
  }

  set(token: string | null): void {
    this.token = token;
  }

  clear(): void {
    this.token = null;
  }

  /** Re-read the session; concurrent callers share one request. */
  refresh(): Promise<SessionState> {
    this.inflight ??= fetchSession(this.fetchImpl)
      .then((state) => {
        this.token = state.csrfToken;
        for (const listener of this.listeners) {
          listener(state);
        }
        return state;
      })
      .finally(() => {
        this.inflight = null;
      });
    return this.inflight;
  }

  /** The current token, fetching the session first if there is none. */
  async ensure(): Promise<string | null> {
    if (this.token !== null) {
      return this.token;
    }
    return (await this.refresh()).csrfToken;
  }

  /** Called with every refreshed session state; returns an unsubscribe function. */
  subscribe(listener: (state: SessionState) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }
}

export const csrfStore = new CsrfTokenStore();
