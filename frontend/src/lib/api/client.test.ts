// SPDX-License-Identifier: AGPL-3.0-or-later
import { CsrfTokenStore } from "../csrf";
import {
  ApiError,
  createApiClient,
  errorMessage,
  isApiError,
  parseApiError,
  problemFromBody,
  unwrap,
  type ApiClient,
} from "./client";

function problem(status: number, body: unknown, contentType = "application/problem+json"): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": contentType } });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

// The generated schema is empty until the backend grows routes; tests call
// paths through an untyped view of the client.
type LooseClient = Record<"GET" | "POST" | "DELETE", (path: string, init?: Record<string, unknown>) => Promise<{
  data?: unknown;
  error?: unknown;
  response: Response;
}>>;

function loose(client: ApiClient): LooseClient {
  return client as unknown as LooseClient;
}

describe("problem-details parsing", () => {
  it("parses an RFC 9457 body into a typed ApiError", async () => {
    const error = await parseApiError(
      problem(404, {
        type: "urn:perfin:problem:account-not-found",
        title: "Account not found",
        status: 404,
        detail: "No such account.",
        instance: "/api/v1/accounts/x",
        request_id: "req-1",
      }),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(404);
    expect(error.slug).toBe("account-not-found");
    expect(error.title).toBe("Account not found");
    expect(error.detail).toBe("No such account.");
    expect(error.instance).toBe("/api/v1/accounts/x");
    expect(error.extensions).toEqual({ request_id: "req-1" });
    expect(error.message).toBe("No such account.");
    expect(errorMessage(error)).toBe("No such account.");
  });

  it("maps validation errors to field messages", () => {
    const error = problemFromBody(422, {
      type: "urn:perfin:problem:validation-error",
      title: "Request validation failed",
      status: 422,
      errors: [
        { loc: ["body", "name"], msg: "Field required", type: "missing" },
        { loc: ["body", "limits", 0, "amount"], msg: "Not a decimal", type: "decimal_parsing" },
        { loc: ["body", "name"], msg: "second message ignored", type: "x" },
        { nonsense: true },
        "junk",
      ],
    });
    expect(error.issues).toHaveLength(3);
    expect(error.fieldErrors()).toEqual({ name: "Field required", "limits.0.amount": "Not a decimal" });
    expect(error.extensions).toEqual({});
  });

  it("uses the HTTP status, not the body's, and clips oversized text", () => {
    const error = problemFromBody(403, { type: "urn:perfin:problem:forbidden", title: "x".repeat(5000), status: 200 });
    expect(error.status).toBe(403);
    expect(error.title.length).toBeLessThanOrEqual(501);
  });

  it("degrades to a generic error for non-problem bodies", async () => {
    const html = new Response("<h1>Bad Gateway</h1>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "content-type": "text/html" },
    });
    const error = await parseApiError(html);
    expect(error.status).toBe(502);
    expect(error.slug).toBe("server-error");
    expect(error.title).toBe("Bad Gateway");
    expect(error.detail).toBeNull();

    const broken = new Response("{not json", { status: 500, headers: { "content-type": "application/json" } });
    expect((await parseApiError(broken)).slug).toBe("server-error");
    expect(problemFromBody(409, ["array"]).slug).toBe("http-409");
    expect(problemFromBody(400, { type: 42, title: null }).type).toBe("about:blank");
  });

  it("does not let a body pollute prototypes", () => {
    const body = JSON.parse('{"title":"t","__proto__":{"polluted":true}}') as unknown;
    const error = problemFromBody(400, body);
    expect(Object.prototype).not.toHaveProperty("polluted");
    expect(error.extensions).not.toHaveProperty("polluted");
  });

  it("gives non-API errors a generic message", () => {
    expect(errorMessage(new Error("SELECT * FROM secrets"))).toBe("Something went wrong.");
    expect(isApiError(new Error("x"))).toBe(false);
  });
});

describe("api client", () => {
  function setup(responder: (request: Request) => Response | Promise<Response>, sessionToken: string | null = "tok-123") {
    const sessionFetch = vi.fn(() =>
      Promise.resolve(sessionToken === null ? new Response(null, { status: 404 }) : json({ csrf_token: sessionToken })),
    );
    const store = new CsrfTokenStore(sessionFetch);
    const requests: Request[] = [];
    const client = createApiClient({
      baseUrl: "https://perfin.test",
      csrf: store,
      fetch: async (request) => {
        requests.push(request.clone());
        return responder(request);
      },
    });
    return { client: loose(client), store, requests, sessionFetch };
  }

  it("sends same-origin credentials and no CSRF header on safe methods", async () => {
    const { client, requests, sessionFetch } = setup(() => json({ ok: true }));
    await expect(unwrap(client.GET("/api/v1/things"))).resolves.toEqual({ ok: true });
    const [request] = requests;
    expect(request?.credentials).toBe("same-origin");
    expect(request?.url).toBe("https://perfin.test/api/v1/things");
    expect(request?.headers.get("X-CSRF-Token")).toBeNull();
    expect(request?.headers.get("Accept")).toContain("application/problem+json");
    expect(sessionFetch).not.toHaveBeenCalled();
  });

  it("adds X-CSRF-Token from the session on unsafe methods, fetching it once", async () => {
    const { client, requests, sessionFetch } = setup(() => json({ id: "1" }, 201));
    await unwrap(client.POST("/api/v1/things", { body: { name: "a" } }));
    await unwrap(client.DELETE("/api/v1/things/1"));
    expect(requests.map((request) => request.headers.get("X-CSRF-Token"))).toEqual(["tok-123", "tok-123"]);
    expect(sessionFetch).toHaveBeenCalledTimes(1);
    expect(sessionFetch).toHaveBeenCalledWith(
      "/api/v1/auth/session",
      expect.objectContaining({ credentials: "same-origin", method: "GET" }),
    );
  });

  it("still sends unsafe requests when the session endpoint is missing (pre-Phase 1)", async () => {
    const { client, requests } = setup(() => json({}), null);
    await unwrap(client.POST("/api/v1/things", { body: {} }));
    expect(requests[0]?.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("throws ApiError from problem responses and drops a rejected CSRF token", async () => {
    const { client, store } = setup(() =>
      problem(403, { type: "urn:perfin:problem:csrf-failed", title: "CSRF check failed", status: 403 }),
    );
    const failure = unwrap(client.POST("/api/v1/things", { body: {} }));
    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ status: 403, slug: "csrf-failed" });
    await vi.waitFor(() => {
      expect(store.get()).toBeNull();
    });
  });

  it("clears the token on 401", async () => {
    const { client, store } = setup(() => problem(401, { type: "urn:perfin:problem:unauthenticated", title: "Sign in" }));
    store.set("stale");
    await expect(unwrap(client.GET("/api/v1/things"))).rejects.toMatchObject({ isUnauthenticated: true });
    expect(store.get()).toBeNull();
  });

  it("turns network failures into a status-0 ApiError", async () => {
    const { client } = setup(() => Promise.reject(new TypeError("Failed to fetch")));
    await expect(unwrap(client.GET("/api/v1/things"))).rejects.toMatchObject({
      status: 0,
      slug: "network-error",
      isNetworkError: true,
    });
  });
});
