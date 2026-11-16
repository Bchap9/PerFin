// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";

import { expectNoAxeViolations } from "../test/axe";
import { SectionNav, type SectionNavItem } from "./SectionNav";

/** Render `ui` on a bare router at `path` (every path matches a splat route). */
function renderAt(path: string, ui: ReactNode) {
  const rootRoute = createRootRoute({
    component: () => (
      <>
        {ui}
        <Outlet />
      </>
    ),
  });
  const anyRoute = createRoute({ getParentRoute: () => rootRoute, path: "$", component: () => null });
  const router = createRouter({
    routeTree: rootRoute.addChildren([anyRoute]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const view = render(<RouterProvider router={router} />);
  return { router, ...view };
}

const PLAN: readonly SectionNavItem[] = [
  { to: "/plan/budgets", label: "Budgets" },
  { to: "/plan/goals", label: "Goals" },
  { to: "/plan/subscriptions", label: "Subscriptions" },
];

const REPORTS: readonly SectionNavItem[] = [
  { to: "/reports", label: "Spending", keepSearch: ["period", "account"] },
  { to: "/reports", label: "Income", search: { tab: "income" }, keepSearch: ["period", "account"] },
  { to: "/reports", label: "Cash flow", search: { tab: "cashflow" }, keepSearch: ["period", "account"] },
  { to: "/reports/net-worth", label: "Net worth", keepSearch: ["period"] },
];

describe("SectionNav", () => {
  it("marks the current item by path", async () => {
    renderAt("/plan/subscriptions/s-1", <SectionNav label="Plan" items={PLAN} />);
    const nav = await screen.findByRole("navigation", { name: "Plan" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Subscriptions" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Budgets" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Goals" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Budgets" })).toHaveAttribute("href", "/plan/budgets");
  });

  it("by tab search value", async () => {
    renderAt("/reports?tab=income", <SectionNav label="Reports" items={REPORTS} />);
    const income = await screen.findByRole("link", { name: "Income" });
    expect(income).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Spending" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("link", { name: "Net worth" })).not.toHaveAttribute("aria-current");
  });

  it("marks the item without a tab when the address has none", async () => {
    renderAt("/reports", <SectionNav label="Reports" items={REPORTS} />);
    expect(await screen.findByRole("link", { name: "Spending" })).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link").filter((link) => link.hasAttribute("aria-current"))).toHaveLength(1);
  });

  it("the longest path wins", async () => {
    renderAt("/reports/net-worth?period=last-year", <SectionNav label="Reports" items={REPORTS} />);
    expect(await screen.findByRole("link", { name: "Net worth" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Spending" })).not.toHaveAttribute("aria-current");
  });

  it("keeps the named search parameters", async () => {
    const user = userEvent.setup();
    const { router } = renderAt(
      "/reports?tab=income&period=last-month&account=a-1&group=payee",
      <SectionNav label="Reports" items={REPORTS} />,
    );
    const cash = await screen.findByRole("link", { name: "Cash flow" });
    const href = new URL(cash.getAttribute("href") ?? "", "http://localhost");
    expect(href.pathname).toBe("/reports");
    expect(Object.fromEntries(href.searchParams)).toEqual({ tab: "cashflow", period: "last-month", account: "a-1" });

    await user.click(screen.getByRole("link", { name: "Spending" }));
    expect(router.state.location.pathname).toBe("/reports");
    expect(router.state.location.search).toEqual({ period: "last-month", account: "a-1" });
  });

  it("a count is in the name", async () => {
    renderAt(
      "/inbox",
      <SectionNav
        label="Inbox"
        items={[
          { to: "/inbox", label: "Needs you", count: 3 },
          { to: "/inbox/history", label: "History" },
        ]}
      />,
    );
    const needs = await screen.findByRole("link", { name: "Needs you, 3" });
    expect(needs).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "History" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = renderAt("/reports?tab=income", <SectionNav label="Reports" items={REPORTS} />);
    await screen.findByRole("navigation", { name: "Reports" });
    await expectNoAxeViolations(container);
  });
});
