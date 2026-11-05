// SPDX-License-Identifier: AGPL-3.0-or-later
import { linkSecret, fragmentParam } from "./fragment";
import { guardRedirect, isBarePath, isPublicPath } from "./guard";
import { returnToFromSearch, safeReturnTo, withReturnTo } from "./returnTo";
import { sessionBody } from "./testing";

describe("safeReturnTo", () => {
  it("keeps same-origin relative paths with their query and fragment", () => {
    expect(safeReturnTo("/settings")).toBe("/settings");
    expect(safeReturnTo("/transactions?month=2026-10#row-3")).toBe("/transactions?month=2026-10#row-3");
    expect(safeReturnTo("/login/link-oidc")).toBe("/login/link-oidc");
  });

  it.each([
    ["absolute URL", "https://evil.example/"],
    ["protocol-relative", "//evil.example/x"],
    ["backslash host", "/\\evil.example"],
    ["script URL", ["javascript", "alert(1)"].join(":")],
    ["relative without slash", "settings"],
    ["control character", "/settings\n/x"],
    ["tab", "/\t/evil.example"],
    ["empty", ""],
    ["not a string", 42],
    ["too long", `/${"a".repeat(3000)}`],
    ["login loop", "/login?return_to=/x"],
    ["mfa loop", "/login/mfa"],
    ["setup", "/setup"],
    // Dot segments the URL parser would resolve into "//host" (review P1-SR2-02).
    ["dot-dot then double slash", "/a/..//evil.example"],
    ["dot then double slash", "/.//evil.example"],
    ["encoded dot-dot", "/%2e%2e//evil.example/x?y#z"],
    ["upper-case encoded dot-dot", "/%2E%2E//evil.example"],
    ["mixed dot-dot", "/a/.%2e//evil.example"],
    ["dot-dot at the end", "/settings/.."],
    ["loop via dot segment", "/x/../login"],
  ])("rejects %s", (_label, value) => {
    expect(safeReturnTo(value)).toBe("/");
  });

  it("never returns or writes out a protocol-relative target", () => {
    for (const value of ["/a/..//evil.example", "/.//evil.example", "/%2e%2e//evil.example/x?y#z"]) {
      expect(safeReturnTo(value).startsWith("//")).toBe(false);
      expect(withReturnTo("/login", value)).toBe("/login");
    }
  });

  it("keeps dots that are not whole segments", () => {
    expect(safeReturnTo("/.well-known/x")).toBe("/.well-known/x");
    expect(safeReturnTo("/files/report..2026")).toBe("/files/report..2026");
    expect(safeReturnTo("/search?q=../x")).toBe("/search?q=../x");
  });

  it("builds and reads return_to parameters", () => {
    expect(withReturnTo("/login", "/settings/security")).toBe("/login?return_to=%2Fsettings%2Fsecurity");
    expect(withReturnTo("/login", "/")).toBe("/login");
    expect(withReturnTo("/login", "https://evil.example")).toBe("/login");
    expect(returnToFromSearch("?return_to=%2Fbudgets%3Fy%3D2026")).toBe("/budgets?y=2026");
    expect(returnToFromSearch("?return_to=%2F%2Fevil.example")).toBe("/");
    expect(returnToFromSearch("")).toBe("/");
  });
});

describe("link secrets", () => {
  it("reads secrets from the fragment, then the query, and refuses odd values", () => {
    expect(fragmentParam("#t=abc_DEF-1", "t")).toBe("abc_DEF-1");
    expect(fragmentParam("t=abc", "t")).toBe("abc");
    expect(linkSecret("token=frag", "?token=query", ["token"])).toBe("frag");
    expect(linkSecret("", "?t=fromquery", ["t", "token"])).toBe("fromquery");
    expect(linkSecret("t=<script>", "", ["t"])).toBeNull();
    expect(linkSecret("", "", ["t"])).toBeNull();
  });
});

describe("route guard", () => {
  const anonymous = { status: "anonymous", csrfToken: "pre" } as const;
  const pending = { status: "mfa_pending", csrfToken: "p", methods: ["totp"] } as const;
  const unavailable = { status: "unavailable", csrfToken: null, httpStatus: null } as const;
  const signedIn = { status: "authenticated", csrfToken: "s", data: sessionBody() } as const;
  const enrolling = { status: "authenticated", csrfToken: "s", data: sessionBody({ enrollmentRequired: true }) } as const;

  it("sends anonymous visitors of private pages to sign-in with return_to", () => {
    expect(guardRedirect(anonymous, "/accounts", "?tab=2", "/accounts?tab=2")).toBe("/login?return_to=%2Faccounts%3Ftab%3D2");
    expect(guardRedirect(anonymous, "/", "", "/")).toBe("/login");
    expect(guardRedirect(anonymous, "/login/enroll-mfa", "", "/login/enroll-mfa")).toBe("/login");
    for (const path of ["/login", "/login/mfa", "/login/link-oidc", "/setup", "/forgot-password", "/reset-password"]) {
      expect(guardRedirect(anonymous, path, "", path)).toBeNull();
    }
  });

  it("sends a pending login to the second-factor step", () => {
    expect(guardRedirect(pending, "/settings", "", "/settings")).toBe("/login/mfa?return_to=%2Fsettings");
    expect(guardRedirect(pending, "/login/mfa", "", "/login/mfa")).toBeNull();
  });

  it("sends an enrolment-only session to enrolment and keeps everyone else", () => {
    expect(guardRedirect(enrolling, "/budgets", "", "/budgets")).toBe("/login/enroll-mfa?return_to=%2Fbudgets");
    expect(guardRedirect(enrolling, "/login/enroll-mfa", "", "/login/enroll-mfa")).toBeNull();
    expect(guardRedirect(enrolling, "/login", "?return_to=%2Fx", "/login?return_to=%2Fx")).toBe("/login/enroll-mfa?return_to=%2Fx");
    expect(guardRedirect(signedIn, "/budgets", "", "/budgets")).toBeNull();
  });

  it("moves a signed-in user off the sign-in pages to a safe target", () => {
    expect(guardRedirect(signedIn, "/login", "?return_to=%2Fbudgets", "/login?return_to=%2Fbudgets")).toBe("/budgets");
    expect(guardRedirect(signedIn, "/login", "?return_to=https%3A%2F%2Fevil.example", "")).toBe("/");
    expect(guardRedirect(signedIn, "/login/enroll-mfa", "", "/login/enroll-mfa")).toBe("/");
  });

  it("does not redirect when the session endpoint cannot be reached", () => {
    expect(guardRedirect(unavailable, "/accounts", "", "/accounts")).toBeNull();
  });

  it("knows which pages are public and which are bare", () => {
    expect(isPublicPath("/login/")).toBe(true);
    expect(isPublicPath("/settings")).toBe(false);
    expect(isBarePath("/login/enroll-mfa")).toBe(true);
    expect(isBarePath("/accounts")).toBe(false);
  });
});
