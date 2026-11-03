// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { safeExternalUrl } from "./AccountMenu";
import { ANON_SESSION, authedSession, json, mockApi, noContent, problem, renderApp } from "./testing";

const HOUSEHOLDS = () =>
  json([
    { id: "h-1", name: "Home", base_currency: "USD", created_at: "2026-01-01T00:00:00Z", role: "owner" },
    { id: "h-2", name: "Parents", base_currency: "EUR", created_at: "2026-01-02T00:00:00Z", role: "viewer" },
    { id: "h-3", name: "Not mine", base_currency: "USD", created_at: "2026-01-03T00:00:00Z", role: null },
  ]);

describe("account menu", () => {
  it("switches household with the CSRF token and refreshes the session", async () => {
    let active = "h-1";
    const api = mockApi({
      "GET /api/v1/auth/session": (call) =>
        authedSession(active === "h-1" ? {} : { householdId: "h-2", householdName: "Parents", role: "viewer", csrfToken: "t2" })(call),
      "GET /api/v1/households": HOUSEHOLDS,
      "POST /api/v1/auth/session/household": (call) => {
        active = "h-2";
        expect(call.body).toEqual({ household_id: "h-2" });
        return json({ household: { id: "h-2", name: "Parents", role: "viewer" }, csrf_token: "t2" });
      },
    });
    renderApp("/");
    const select = await screen.findByLabelText("Household");
    // Only households the user is a member of are offered.
    expect(Array.from((select as HTMLSelectElement).options).map((option) => option.text)).toEqual(["Home", "Parents"]);
    const user = userEvent.setup();
    await user.selectOptions(select, "h-2");
    expect(await screen.findByText("Switched to Parents")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Household")).toHaveValue("h-2");
    });
    expect(api.callsTo("POST", "/api/v1/auth/session/household")[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
  });

  it("shows the household name without a switcher for a single household", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession(),
      "GET /api/v1/households": () => json([{ id: "h-1", name: "Home", base_currency: "USD", created_at: "2026-01-01T00:00:00Z", role: "owner" }]),
    });
    renderApp("/");
    expect(await screen.findByText("Home", { selector: ".account-menu__household" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Household")).not.toBeInTheDocument();
  });

  it("signs out a local session and returns to sign-in", async () => {
    let signedOut = false;
    const api = mockApi({
      "GET /api/v1/auth/session": (call) => (signedOut ? ANON_SESSION(call) : authedSession()(call)),
      "GET /api/v1/households": HOUSEHOLDS,
      "POST /api/v1/auth/logout": () => {
        signedOut = true;
        return noContent();
      },
    });
    const { router } = renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/login");
    });
    expect(api.callsTo("POST", "/api/v1/auth/logout")[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
  });

  it("ends an OIDC session at the identity provider", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { href: window.location.href, origin: window.location.origin, assign });
    let signedOut = false;
    mockApi({
      "GET /api/v1/auth/session": (call) => (signedOut ? ANON_SESSION(call) : authedSession({ authMethod: "oidc" })(call)),
      "GET /api/v1/households": HOUSEHOLDS,
      "POST /api/v1/auth/oidc/logout": () => {
        signedOut = true;
        return json({ redirect_url: "https://idp.example/end-session?id_token_hint=x" });
      },
    });
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith("https://idp.example/end-session?id_token_hint=x");
    });
  });

  it("reports a failed sign-out and stays", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession(),
      "GET /api/v1/households": HOUSEHOLDS,
      "POST /api/v1/auth/logout": () => problem(500, "server-error"),
    });
    const { router } = renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("Sign-out failed")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("only follows http(s) end-session URLs", () => {
    expect(safeExternalUrl("https://idp.example/logout")).toBe("https://idp.example/logout");
    expect(safeExternalUrl(["javascript", "alert(1)"].join(":"))).toBeNull();
    expect(safeExternalUrl("data:text/html,x")).toBeNull();
    expect(safeExternalUrl("/relative")).toBeNull();
    expect(safeExternalUrl(null)).toBeNull();
  });
});
