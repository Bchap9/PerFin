// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ANON_SESSION, authedSession, json, mockApi, problem, renderApp } from "../auth/testing";

const SETUP_OUT = {
  user: { id: "u-1", username: "admin", display_name: "Ada", is_instance_admin: true },
  household: { id: "h-1", name: "Lovelace", base_currency: "EUR", role: "owner" },
  csrf_token: "session-token",
};

async function fillForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(/^Username/), "Admin");
  await user.type(screen.getByLabelText(/^Your name/), "Ada");
  await user.type(screen.getByLabelText(/^Password/), "a long admin passphrase");
  await user.type(screen.getByLabelText(/^Repeat the password/), "a long admin passphrase");
  await user.type(screen.getByLabelText(/^Household name/), "Lovelace");
  await user.clear(screen.getByLabelText(/^Base currency/));
  await user.type(screen.getByLabelText(/^Base currency/), "eur");
}

describe("first-run setup", () => {
  it("creates the administrator with the fragment token and signs in", async () => {
    let done = false;
    const api = mockApi({
      "GET /api/v1/auth/session": (call) => (done ? authedSession()(call) : ANON_SESSION(call)),
      "GET /api/v1/setup/status": () => json({ setup_required: !done }),
      "POST /api/v1/setup": () => {
        done = true;
        return json(SETUP_OUT, 201);
      },
    });
    const { router } = renderApp("/setup#t=setup_TOKEN-1");
    // The one-time token leaves the address bar at once.
    await waitFor(() => {
      expect(router.state.location.hash).toBe("");
    });
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    const user = userEvent.setup();
    await fillForm(user);
    await user.click(screen.getByRole("button", { name: "Create administrator" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/");
    });
    const [call] = api.callsTo("POST", "/api/v1/setup");
    expect(call?.body).toEqual({
      token: "setup_TOKEN-1",
      username: "admin",
      display_name: "Ada",
      password: "a long admin passphrase",
      household_name: "Lovelace",
      base_currency: "EUR",
    });
    expect(call?.headers.get("X-CSRF-Token")).toBe("pre-token");
  });

  it("accepts the token from the query string too", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/setup/status": () => json({ setup_required: true }),
      "POST /api/v1/setup": () => problem(403, "invalid-setup-token"),
    });
    const { router } = renderApp("/setup?t=from_query");
    await waitFor(() => {
      expect(router.state.location.searchStr).toBe("");
    });
    const user = userEvent.setup();
    await fillForm(user);
    await user.click(screen.getByRole("button", { name: "Create administrator" }));
    expect(await screen.findByText(/The setup link is wrong or has expired/)).toHaveAttribute("role", "alert");
    expect(api.callsTo("POST", "/api/v1/setup")[0]?.body).toMatchObject({ token: "from_query" });
  });

  it("validates the form before calling the server", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/setup/status": () => json({ setup_required: true }),
    });
    renderApp("/setup#t=abc");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Create administrator" }));
    expect(screen.getByText(/Use 1-64 lower-case letters/)).toBeInTheDocument();
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
    expect(screen.getByText(/Name the household/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/setup")).toHaveLength(0);
  });

  it("asks for the setup link when there is no token", async () => {
    mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/setup/status": () => json({ setup_required: true }),
    });
    renderApp("/setup");
    expect(await screen.findByText(/Open the setup link printed in the server log/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create administrator" })).not.toBeInTheDocument();
  });

  it("points to sign-in once setup is done", async () => {
    mockApi({
      "GET /api/v1/auth/session": ANON_SESSION,
      "GET /api/v1/setup/status": () => json({ setup_required: false }),
    });
    renderApp("/setup#t=abc");
    expect(await screen.findByRole("heading", { name: "PerFin is set up" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });
});
