// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";

import { expectNoAxeViolations } from "../test/axe";
import { Button } from "./Button";
import { PageHeader, SectionHeadingContext } from "./PageHeader";

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
  return render(<RouterProvider router={router} />);
}

describe("PageHeader", () => {
  it("h1 outside a section, h2 inside", () => {
    const { rerender } = render(
      <PageHeader title="Budgets" description="What you plan to spend each month." actions={<Button>New budget</Button>} />,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Budgets" })).toBeInTheDocument();
    expect(screen.getByText("What you plan to spend each month.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New budget" })).toBeInTheDocument();

    rerender(
      <SectionHeadingContext.Provider value>
        <PageHeader title="Budgets" />
      </SectionHeadingContext.Provider>,
    );
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Budgets" })).toBeInTheDocument();
  });

  it("breadcrumbs are a nav with the last item current", async () => {
    renderAt(
      "/accounts/a-1",
      <PageHeader
        title="Everyday checking"
        breadcrumbs={[{ to: "/accounts", label: "Accounts" }, { to: "/accounts?group=cash", label: "Cash" }, { label: "Everyday checking" }]}
      />,
    );
    const nav = await screen.findByRole("navigation", { name: "Breadcrumbs" });
    const list = within(nav).getByRole("list");
    const entries = within(list).getAllByRole("listitem");
    expect(entries).toHaveLength(3);
    expect(within(nav).getByRole("link", { name: "Accounts" })).toHaveAttribute("href", "/accounts");
    expect(within(nav).getByRole("link", { name: "Cash" })).toHaveAttribute("href", "/accounts?group=cash");
    const last = within(entries[2] ?? nav).getByText("Everyday checking");
    expect(last).toHaveAttribute("aria-current", "page");
    expect(within(nav).queryByRole("link", { name: "Everyday checking" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Everyday checking" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = renderAt(
      "/plan/budgets/b-1",
      <PageHeader
        title="Groceries"
        description="Monthly budget"
        actions={<Button>Edit</Button>}
        breadcrumbs={[{ to: "/plan/budgets", label: "Budgets" }, { label: "Groceries" }]}
      />,
    );
    await screen.findByRole("navigation", { name: "Breadcrumbs" });
    await expectNoAxeViolations(container);
  });
});
