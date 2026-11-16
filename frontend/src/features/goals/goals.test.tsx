// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, renderApp } from "../auth/testing";
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
    renderApp("/plan/goals");
    const user = userEvent.setup();
    expect(await screen.findByRole("progressbar", { name: "Emergency fund: 25 percent complete" })).toBeInTheDocument();
    // Off track: the projected month is not called "on pace" next to the "Behind" badge.
    expect(screen.getByText("Projected for July 2027")).toBeInTheDocument();
    expect(screen.queryByText(/On pace for/)).toBeNull();
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

  it("an on-track goal is on pace for its projected month", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/goals": () => json({ items: [goal({ on_track: true, projected_completion: "2027-05-20" })], next_cursor: null }),
    });
    renderApp("/plan/goals");
    expect(await screen.findByText("On pace for May 2027")).toBeInTheDocument();
    expect(screen.queryByText(/Projected for/)).toBeNull();
  });

  it("a viewer with no goals is not asked to set one", async () => {
    mockApi({
      ...referenceHandlers(),
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/goals": () => json({ items: [], next_cursor: null }),
    });
    renderApp("/plan/goals");
    expect(await screen.findByText("No goals yet")).toBeInTheDocument();
    expect(screen.getByText("No one in this household has set a goal yet.")).toBeInTheDocument();
    expect(screen.queryByText(/Set a target/)).toBeNull();
  });

  it("says nothing about pace when there is no projection", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/goals": () => json({ items: [goal({ projected_completion: null })], next_cursor: null }),
    });
    renderApp("/plan/goals");
    expect(await screen.findByText(/Not enough history/)).toBeInTheDocument();
    expect(screen.queryByText(/On pace for|Projected for/)).toBeNull();
  });

  it("a viewer sees no New goal", async () => {
    mockApi({
      ...referenceHandlers(),
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/goals": () => json({ items: [goal()], next_cursor: null }),
      "GET /api/v1/goals/:id/contributions": () =>
        json({ items: [{ id: "k-1", goal_id: "g-1", amount: money("100.00"), contributed_on: "2026-10-01", memo: null, created_at: STAMP }], next_cursor: null }),
    });
    renderApp("/plan/goals");
    expect(await screen.findByRole("progressbar", { name: "Emergency fund: 25 percent complete" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New goal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit Emergency fund" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Archive Emergency fund" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete Emergency fund" })).toBeNull();
    // The contributions stay readable; adding and removing them does not show.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Contributions to Emergency fund" }));
    const dialog = await screen.findByRole("dialog", { name: "Contributions to Emergency fund" });
    expect(await within(dialog).findByText("$100.00")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Add contribution" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /^Remove contribution/ })).toBeNull();
  });

  it("has no axe violations", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/goals": () => json({ items: [goal()], next_cursor: null }),
    });
    const { container } = renderApp("/plan/goals");
    await screen.findByRole("progressbar", { name: "Emergency fund: 25 percent complete" });
    await expectNoAxeViolations(container);
  });
});
