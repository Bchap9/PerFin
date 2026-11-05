// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp } from "../auth/testing";

const PREFERENCES = { display_name: "Alice", locale: "en-US", theme: "system", timezone: "UTC" };

describe("profile and settings overview", () => {
  it("links to every settings page", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession(), "GET /api/v1/households": () => json([]) });
    renderApp("/settings");
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Profile" })).toHaveAttribute("href", "/settings/profile");
    expect(screen.getByRole("link", { name: "Security" })).toHaveAttribute("href", "/settings/security");
    expect(screen.getByRole("link", { name: "Household members" })).toHaveAttribute("href", "/settings/household");
  });

  it("saves the profile with the CSRF header and applies the theme", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession(),
      "GET /api/v1/households": () => json([]),
      "GET /api/v1/settings/user": () => json(PREFERENCES),
      "PATCH /api/v1/settings/user": (call) => json({ ...PREFERENCES, ...(call.body as object) }),
    });
    renderApp("/settings/profile");
    const user = userEvent.setup();
    const name = await screen.findByLabelText(/^Display name/);
    expect(name).toHaveValue("Alice");
    await user.clear(name);
    await user.type(name, "Alice Liddell");
    await user.selectOptions(screen.getByRole("combobox", { name: /^Theme/ }), "dark");
    await user.clear(screen.getByLabelText(/^Time zone/));
    await user.type(screen.getByLabelText(/^Time zone/), "Europe/Berlin");
    await user.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByText("Profile saved")).toBeInTheDocument();
    const [call] = api.callsTo("PATCH", "/api/v1/settings/user");
    expect(call?.body).toEqual({ display_name: "Alice Liddell", locale: "en-US", theme: "dark", timezone: "Europe/Berlin" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    await waitFor(() => {
      expect(document.documentElement.dataset["theme"]).toBe("dark");
    });
  });

  it("shows validation errors from the server on their fields", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession(),
      "GET /api/v1/households": () => json([]),
      "GET /api/v1/settings/user": () => json(PREFERENCES),
      "PATCH /api/v1/settings/user": () =>
        problem(422, "validation-error", { errors: [{ loc: ["body", "timezone"], msg: "unknown time zone", type: "value_error" }] }),
    });
    renderApp("/settings/profile");
    const user = userEvent.setup();
    await user.clear(await screen.findByLabelText(/^Time zone/));
    await user.type(screen.getByLabelText(/^Time zone/), "Mars/Base");
    await user.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByText("unknown time zone")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Time zone/)).toHaveAttribute("aria-invalid", "true");
  });
});
