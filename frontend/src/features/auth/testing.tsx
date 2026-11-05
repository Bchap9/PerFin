// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Test helpers for the auth, setup and settings features (imported by tests
 * only): a global `fetch` mock routed by "METHOD /path" that records every
 * call with its headers and body, response builders, session bodies and an
 * app renderer on a memory history.
 */
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";

import { createAppRouter } from "../../app/router";
import { csrfStore } from "../../lib/csrf";

export interface ApiCall {
  method: string;
  path: string;
  search: string;
  headers: Headers;
  body: unknown;
}

export type Handler = (call: ApiCall) => Response | Promise<Response>;

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

export function problem(status: number, slug: string, extra: Record<string, unknown> = {}): Response {
  return json({ type: `urn:perfin:problem:${slug}`, title: slug, status, detail: `detail of ${slug}`, ...extra }, status);
}

export const ANON_SESSION: Handler = () =>
  json({ type: "urn:perfin:problem:unauthenticated", title: "Not authenticated", status: 401, authenticated: false, csrf_token: "pre-token" }, 401);

export function pendingSession(methods: string[] = ["totp"]): Handler {
  return () =>
    json(
      {
        type: "urn:perfin:problem:mfa-required",
        title: "Second factor required",
        status: 401,
        authenticated: false,
        csrf_token: "pending-token",
        mfa: { pending: true, methods },
      },
      401,
    );
}

export interface SessionOptions {
  userId?: string;
  username?: string;
  displayName?: string;
  role?: "owner" | "admin" | "member" | "viewer";
  household?: boolean;
  householdId?: string;
  householdName?: string;
  enrollmentRequired?: boolean;
  enrolled?: boolean;
  authMethod?: "local" | "oidc" | "breakglass" | "bootstrap" | "token";
  csrfToken?: string;
  aiClientsActive?: boolean;
  instanceAdmin?: boolean;
}

export function sessionBody(options: SessionOptions = {}): Record<string, unknown> {
  return {
    authenticated: true,
    user: {
      id: options.userId ?? "u-1",
      username: options.username ?? "alice",
      display_name: options.displayName ?? "Alice",
      is_instance_admin: options.instanceAdmin ?? false,
    },
    household:
      options.household === false
        ? null
        : { id: options.householdId ?? "h-1", name: options.householdName ?? "Home", role: options.role ?? "owner" },
    csrf_token: options.csrfToken ?? "session-token",
    mfa: {
      enrolled: options.enrolled ?? true,
      verified: true,
      verified_at: "2026-11-03T10:00:00Z",
      required: true,
      enrollment_required: options.enrollmentRequired ?? false,
    },
    auth_method: options.authMethod ?? "local",
    ai_clients_active: options.aiClientsActive ?? false,
  };
}

export function authedSession(options: SessionOptions = {}): Handler {
  return () => json(sessionBody(options));
}

function matches(pattern: string, path: string): boolean {
  const want = pattern.split("/");
  const got = path.split("/");
  return want.length === got.length && want.every((part, index) => part.startsWith(":") || part === got.at(index));
}

async function readBody(request: Request): Promise<unknown> {
  const text = await request.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/**
 * Route `fetch` to `handlers` ("GET /api/v1/auth/session": handler; path
 * segments starting with ":" match anything). Unrouted calls answer 404.
 */
export function mockApi(handlers: Record<string, Handler>) {
  const calls: ApiCall[] = [];
  const routes = new Map(Object.entries(handlers));
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), "http://localhost"), init);
    const url = new URL(request.url);
    const call: ApiCall = {
      method: request.method.toUpperCase(),
      path: url.pathname,
      search: url.search,
      headers: request.headers,
      body: await readBody(request),
    };
    calls.push(call);
    for (const [key, handler] of routes) {
      const [method = "", pattern = ""] = key.split(" ");
      if (method === call.method && matches(pattern, call.path)) {
        return handler(call);
      }
    }
    return new Response(null, { status: 404, statusText: "Not Found" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    fetchMock,
    /** Replace or add a handler mid-test. */
    on(key: string, handler: Handler) {
      routes.set(key, handler);
    },
    callsTo(method: string, path: string): ApiCall[] {
      return calls.filter((call) => call.method === method && matches(path, call.path));
    },
  };
}

/** Render the whole app at `path` with a fresh query client and CSRF store. */
export function renderApp(path: string) {
  csrfStore.clear();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createAppRouter({ queryClient }, createMemoryHistory({ initialEntries: [path] }));
  const view = render(<RouterProvider router={router} />);
  return { router, queryClient, ...view };
}

/** The current location as "path?query". */
export function currentHref(router: ReturnType<typeof createAppRouter>): string {
  return `${router.state.location.pathname}${router.state.location.searchStr}`;
}
