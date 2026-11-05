// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ANON_SESSION, authedSession, json, mockApi, noContent, problem, renderApp } from "./testing";

describe("password reset", () => {
  it("requests a reset link and always gives the same answer", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "POST /api/v1/auth/password-reset/request": () => json({ status: "accepted" }, 202),
    });
    renderApp("/forgot-password");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Username or email/), "alice@example.test");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("heading", { name: "Check your email" })).toBeInTheDocument();
    const [call] = api.callsTo("POST", "/api/v1/auth/password-reset/request");
    expect(call?.body).toEqual({ identifier: "alice@example.test" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("pre-token");
  });

  it("reads the token from the fragment, strips it from the address and sends it in the body", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "POST /api/v1/auth/password-reset/confirm": () => noContent(),
    });
    const { router } = renderApp("/reset-password#token=reset_TOKEN-123");
    await waitFor(() => {
      expect(router.state.location.hash).toBe("");
    });
    expect(router.state.location.pathname).toBe("/reset-password");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^New password/), "a much longer passphrase");
    await user.type(screen.getByLabelText(/^Repeat the new password/), "a much longer passphrase");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByRole("heading", { name: "Password changed" })).toBeInTheDocument();
    const [call] = api.callsTo("POST", "/api/v1/auth/password-reset/confirm");
    expect(call?.body).toEqual({ token: "reset_TOKEN-123", new_password: "a much longer passphrase" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("pre-token");
    // The token never travels in a URL.
    expect(api.calls.every((each) => !each.search.includes("reset_TOKEN"))).toBe(true);
  });

  it("checks the new password locally and shows the server's refusal", async () => {
    mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "POST /api/v1/auth/password-reset/confirm": () => problem(400, "invalid-reset-token"),
    });
    renderApp("/reset-password#token=abc");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^New password/), "short");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText(/^New password/));
    await user.type(screen.getByLabelText(/^New password/), "long enough password");
    await user.type(screen.getByLabelText(/^Repeat the new password/), "long enough passwerd");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(screen.getByText("The passwords do not match.")).toBeInTheDocument();

    await user.clear(screen.getByLabelText(/^Repeat the new password/));
    await user.type(screen.getByLabelText(/^Repeat the new password/), "long enough password");
    await user.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/This reset link is invalid, expired or already used/)).toHaveAttribute("role", "alert");
  });

  it("explains a link without a token", async () => {
    mockApi({ "GET /api/v1/auth/session": ANON_SESSION });
    renderApp("/reset-password");
    expect(await screen.findByText(/This reset link is incomplete/)).toBeInTheDocument();
  });
});

describe("OIDC account link", () => {
  it("links after a fresh local sign-in", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession(),
      "GET /api/v1/auth/oidc/link": () => json({ email: "alice@example.test", expires_at: "2026-11-03T10:10:00Z" }),
      "POST /api/v1/auth/oidc/link": () => noContent(),
    });
    renderApp("/login/link-oidc");
    expect(await screen.findByText("alice@example.test")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Link accounts" }));
    expect(await screen.findByRole("heading", { name: "Accounts linked" })).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/auth/oidc/link")[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
  });

  it("asks for the local password first when nobody is signed in", async () => {
    mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/auth/oidc/link": () => json({ email: "alice@example.test", expires_at: "2026-11-03T10:10:00Z" }),
    });
    renderApp("/login/link-oidc");
    expect(await screen.findByRole("form", { name: "Sign in with your password" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link accounts" })).not.toBeInTheDocument();
  });

  it("says when there is no pending link", async () => {
    mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/auth/oidc/link": () => problem(404, "no-pending-link"),
    });
    renderApp("/login/link-oidc");
    expect(await screen.findByText(/There is no pending link/)).toBeInTheDocument();
  });
});
