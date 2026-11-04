// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { ACCOUNT_CHECKING, money, ownerSession, referenceHandlers } from "../planning/testing";
import type { Goal } from "./api";
import { goalBarWidth, projectionText } from "./progress";

const STAMP = "2026-11-01T10:00:00Z";

function goal(extra: Partial<Goal["progress"]> = {}, patch: Partial<Goal> = {}): Goal {
  return {
    id: "g-1",
    name: "Emergency fund",
    target: money("10000.00"),
    target_date: "2027-06-01",
    archived: false,
    achieved_at: null,
    account_ids: [ACCOUNT_CHECKING],
    created_at: STAMP,
    updated_at: STAMP,
    progress: {
      accounts_balance: money("2500.00"),
      as_of: "2026-11-03",
      contributed: money("2500.00"),
      monthly_rate: money("500.00"),
      on_track: false,
      percent: "25.0",
      projected_completion: "2027-07-15",
      remaining: money("7500.00"),
      ...extra,
    },
    ...patch,
  };
}

describe("goal progress", () => {
  it("bounds the bar and words the projection", () => {
    expect(goalBarWidth(goal({ percent: "25.0" }))).toBe(25);
    expect(goalBarWidth(goal({ percent: "130" }))).toBe(100);
    expect(goalBarWidth(goal({ percent: "-4" }))).toBe(0);
    expect(projectionText(goal())).toMatch(/reach it on Jul 15, 2027, after the Jun 1, 2027 target/);
    expect(projectionText(goal({ on_track: true }))).toBe("At the current pace you would reach it on Jul 15, 2027.");
    expect(projectionText(goal({ projected_completion: null }))).toMatch(/Not enough history/);
    expect(projectionText(goal({}, { achieved_at: STAMP }))).toBe("Goal reached.");
  });
});

describe("goals page", () => {
  it("lists goals with progress and creates one in the household currency", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/goals": () => json({ items: [goal()], next_cursor: null }),
      "POST /api/v1/goals": () => json(goal(), 201),
    });
    renderApp("/goals");
    const user = userEvent.setup();
    expect(await screen.findByRole("img", { name: "Emergency fund: 25 percent complete" })).toBeInTheDocument();
    expect(screen.getByText("Behind")).toBeInTheDocument();
    expect(screen.getByText(/after the Jun 1, 2027 target/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New goal" }));
    const dialog = await screen.findByRole("dialog", { name: "New goal" });
    await user.click(within(dialog).getByRole("button", { name: "Save goal" }));
    expect(await within(dialog).findByText("Enter a name.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Name/), "Holiday");
    await user.type(within(dialog).getByLabelText(/^Target amount/), "2400.50");
    await user.click(await within(dialog).findByRole("checkbox", { name: "Checking" }));
    await user.click(within(dialog).getByRole("button", { name: "Save goal" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/goals")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/goals")[0]?.body).toEqual({
      name: "Holiday",
      target: { amount: "2400.50", currency: "USD" },
      target_date: null,
      account_ids: [ACCOUNT_CHECKING],
    });
  });
});
