// SPDX-License-Identifier: AGPL-3.0-or-later
import { CsrfTokenStore, fetchSession, sessionFromResponse } from "./csrf";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

describe("session parsing", () => {
  it("reads an authenticated session and its token", async () => {
    const state = await sessionFromResponse(json({ user: { id: "u" }, role: "owner", csrf_token: "abc_DEF-123" }));
    expect(state).toMatchObject({ status: "authenticated", csrfToken: "abc_DEF-123" });
  });

  it("treats 401 and authenticated:false as anonymous, keeping a pre-auth token", async () => {
    expect(await sessionFromResponse(json({ title: "Unauthorized" }, 401))).toEqual({
      status: "anonymous",
      csrfToken: null,
    });
    expect(await sessionFromResponse(json({ authenticated: false, csrf_token: "pre" }))).toEqual({
      status: "anonymous",
      csrfToken: "pre",
    });
  });

  it("is unavailable on 404, server errors, non-JSON and network failures", async () => {
    expect(await sessionFromResponse(new Response(null, { status: 404 }))).toMatchObject({
      status: "unavailable",
      httpStatus: 404,
    });
    expect(await sessionFromResponse(new Response("oops", { status: 200 }))).toMatchObject({ status: "unavailable" });
    expect(await fetchSession(() => Promise.reject(new TypeError("offline")))).toEqual({
      status: "unavailable",
      csrfToken: null,
      httpStatus: null,
    });
  });

  it("ignores malformed tokens", async () => {
    for (const token of ["", "a b", "x".repeat(300), 42, "<script>"]) {
      const state = await sessionFromResponse(json({ csrf_token: token }));
      expect(state.csrfToken).toBeNull();
    }
  });
});

describe("CsrfTokenStore", () => {
  it("shares one in-flight session request and notifies subscribers", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(json({ csrf_token: "t1" })));
    const store = new CsrfTokenStore(fetchImpl);
    const seen: string[] = [];
    const unsubscribe = store.subscribe((state) => seen.push(state.status));
    const [a, b] = await Promise.all([store.ensure(), store.ensure()]);
    expect(a).toBe("t1");
    expect(b).toBe("t1");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(await store.ensure()).toBe("t1");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(seen).toEqual(["authenticated"]);
    unsubscribe();
    store.clear();
    await store.ensure();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(seen).toEqual(["authenticated"]);
  });
});
