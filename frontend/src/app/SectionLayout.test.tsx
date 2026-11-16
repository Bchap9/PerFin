// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";

import { mockApi, renderApp } from "../features/auth/testing";
import { ownerSession, referenceHandlers } from "../features/planning/testing";
import { expectNoAxeViolations } from "../test/axe";

describe("section layout", () => {
  it("renders the section h1 and its tab bar", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    renderApp("/plan/goals");
    expect(await screen.findByRole("heading", { level: 1, name: "Plan" })).toBeInTheDocument();
    const tabs = screen.getByRole("navigation", { name: "Plan" });
    expect(
      within(tabs)
        .getAllByRole("link")
        .map((link) => link.textContent),
    ).toEqual(["Subscriptions", "Budgets", "Goals", "Upcoming"]);
    expect(within(tabs).getByRole("link", { name: "Goals" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Budgets" })).toHaveAttribute("href", "/plan/budgets");
  });

  it("a detail page gets neither", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    renderApp("/plan/budgets/b-1");
    // The budget page's own heading, as an h1 (not the 404 page's).
    expect(await screen.findByRole("heading", { level: 1, name: "Budget" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Plan" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Plan" })).not.toBeInTheDocument();
  });

  it("a page header inside renders h2", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    renderApp("/plan/goals");
    expect(await screen.findByRole("heading", { level: 2, name: "Goals" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1, name: "Goals" })).not.toBeInTheDocument();
  });

  it("the Inbox tab bar carries the count in its name", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/inbox/summary": () =>
        new Response(JSON.stringify({ cards: 3, documents: 2, loose: 1, subscriptions: 0, suggestions: 0, uncategorised: 4 }), {
          headers: { "content-type": "application/json" },
        }),
    });
    renderApp("/inbox/history");
    const tabs = await screen.findByRole("navigation", { name: "Inbox" });
    expect(await within(tabs).findByRole("link", { name: "Needs you, 3" })).toHaveAttribute("href", "/inbox");
    expect(within(tabs).getByRole("link", { name: "History" })).toHaveAttribute("aria-current", "page");
  });

  it("has no axe violations", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { container } = renderApp("/plan/goals");
    await screen.findByRole("heading", { level: 2, name: "Goals" });
    await expectNoAxeViolations(container);
  });
});
