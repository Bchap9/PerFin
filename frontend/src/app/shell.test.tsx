// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient } from "@tanstack/react-query";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, Outlet, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp } from "../features/auth/testing";
import { ApiError } from "../lib/api/client";
import { expectNoAxeViolations } from "../test/axe";
import { ErrorBoundary } from "./ErrorBoundary";
import { RouteErrorPage } from "./pages";
import { createAppRouter } from "./router";

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createAppRouter({ queryClient }, createMemoryHistory({ initialEntries: [path] }));
  render(<RouterProvider router={router} />);
  return router;
}

const SUMMARY = { documents: 2, loose: 1, subscriptions: 0, suggestions: 0, uncategorised: 0 };

function railNames(): string[] {
  const rail = screen.getByRole("navigation", { name: "Sections" });
  return within(rail)
    .getAllByRole("link")
    .map((link) => link.textContent);
}

describe("app shell", () => {
  it("the rail lists the six sections in order, then Settings", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    renderApp("/settings");
    await screen.findByRole("navigation", { name: "Sections" });
    expect(railNames()).toEqual(["Home", "Inbox", "Transactions", "Accounts", "Plan", "Reports", "Settings"]);
    const rail = screen.getByRole("navigation", { name: "Sections" });
    expect(within(rail).getByRole("link", { name: "Plan" })).toHaveAttribute("href", "/plan/subscriptions");
    expect(screen.getByRole("link", { name: "PerFin" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("button", { name: /^Theme: / })).toBeInTheDocument();
  });

  it("the current section is aria-current", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    const user = userEvent.setup();
    const router = renderAt("/tax");
    const rail = await screen.findByRole("navigation", { name: "Sections" });
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/reports/tax");
    });
    expect(within(rail).getByRole("link", { name: "Reports" })).toHaveAttribute("aria-current", "page");
    expect(within(rail).getByRole("link", { name: "Home" })).not.toHaveAttribute("aria-current");

    await user.click(within(rail).getByRole("link", { name: "Plan" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Plan" })).toBeInTheDocument();
    expect(within(rail).getByRole("link", { name: "Plan" })).toHaveAttribute("aria-current", "page");
    expect(within(rail).getByRole("link", { name: "Reports" })).not.toHaveAttribute("aria-current");

    await user.click(within(rail).getByRole("link", { name: "Settings" }));
    await waitFor(() => {
      expect(within(rail).getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    });
    expect(within(rail).getByRole("link", { name: "Plan" })).not.toHaveAttribute("aria-current");
  });

  it("the Inbox badge shows the card count and hides at zero and on error", async () => {
    let answer: () => Response = () => json({ cards: 3, ...SUMMARY });
    mockApi({ "GET /api/v1/auth/session": authedSession(), "GET /api/v1/inbox/summary": () => answer() });
    const first = renderApp("/settings");
    const rail = await screen.findByRole("navigation", { name: "Sections" });
    expect(await within(rail).findByRole("link", { name: "Inbox, 3 things need you" })).toHaveAttribute("href", "/inbox");
    first.unmount();

    answer = () => json({ cards: 0, ...SUMMARY });
    const second = renderApp("/settings");
    const quiet = await screen.findByRole("navigation", { name: "Sections" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(within(quiet).getByRole("link", { name: "Inbox" })).toBeInTheDocument();
    second.unmount();

    answer = () => problem(500, "server-error");
    renderApp("/settings");
    const failed = await screen.findByRole("navigation", { name: "Sections" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(within(failed).getByRole("link", { name: "Inbox" })).toBeInTheDocument();
    expect(within(failed).queryByText(/0/)).not.toBeInTheDocument();
  });

  it("bare pages have no rail", async () => {
    renderAt("/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Sections" })).not.toBeInTheDocument();
  });

  it("the skip link targets main", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    renderApp("/settings");
    await screen.findByRole("navigation", { name: "Sections" });
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });

  it("the account menu has Settings and Sign out", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession() });
    const user = userEvent.setup();
    const { router } = renderApp("/plan/goals");
    await user.click(await screen.findByRole("button", { name: "Account: Alice" }));
    const menu = screen.getByRole("menu", { name: "Account: Alice" });
    expect(
      within(menu)
        .getAllByRole("menuitem")
        .map((item) => item.textContent),
    ).toEqual(["Profile", "Settings", "Sign out"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Settings" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/settings");
    });
  });

  it("has no axe violations", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession(), "GET /api/v1/inbox/summary": () => json({ cards: 3, ...SUMMARY }) });
    const { container } = renderApp("/settings");
    const rail = await screen.findByRole("navigation", { name: "Sections" });
    await within(rail).findByRole("link", { name: "Inbox, 3 things need you" });
    await expectNoAxeViolations(container);
  });

  it("renders a 404 page for unknown paths", async () => {
    renderAt("/no/such/page");
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
  });

  it("sends an anonymous visitor to sign-in, remembering where they were going", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ title: "Unauthorized", status: 401 }), {
            status: 401,
            headers: { "content-type": "application/problem+json" },
          }),
        ),
      ),
    );
    const router = renderAt("/settings");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/login");
    expect(new URLSearchParams(router.state.location.searchStr).get("return_to")).toBe("/settings");
    expect(screen.queryByRole("navigation", { name: "Sections" })).not.toBeInTheDocument();
  });

  it("does not show sign-in while the session endpoint does not exist (404)", async () => {
    renderAt("/settings");
    expect(await screen.findByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    await vi.waitFor(() => {
      expect(fetch).toHaveBeenCalled();
    });
    expect(screen.queryByRole("link", { name: "Sign in" })).not.toBeInTheDocument();
  });
});

describe("error handling", () => {
  function Boom({ error }: { error: unknown }): never {
    throw error;
  }

  // React (dev build) rethrows caught render errors through a window "error"
  // event and logs them; keep the expected noise out of the test output.
  const swallow = (event: ErrorEvent): void => {
    event.preventDefault();
  };

  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    window.addEventListener("error", swallow);
  });

  afterEach(() => {
    window.removeEventListener("error", swallow);
  });

  it("shows a generic message without internals for unexpected errors", async () => {
    const user = userEvent.setup();
    let fail = true;
    function MaybeBoom() {
      if (fail) {
        throw new Error("TypeError at db.query(SELECT secret)");
      }
      return <p>Recovered</p>;
    }
    render(
      <ErrorBoundary>
        <MaybeBoom />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole("alert");
    expect(within(alert).getByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(alert).toHaveTextContent("Something went wrong.");
    expect(alert).not.toHaveTextContent("SELECT");
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("Recovered")).toBeInTheDocument();
  });

  it("shows the problem detail of an ApiError and a sign-in prompt on 401", () => {
    const { unmount } = render(
      <ErrorBoundary>
        <Boom
          error={new ApiError({ status: 409, type: "urn:perfin:problem:conflict", title: "Conflict", detail: "Already imported." })}
        />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Already imported.");
    unmount();
    render(
      <ErrorBoundary>
        <Boom error={new ApiError({ status: 401, type: "urn:perfin:problem:unauthenticated", title: "Sign in" })} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("heading", { name: "Your session has ended" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
  });

  it("catches errors thrown by a route with the route error page", async () => {
    const root = createRootRoute({ component: Outlet, errorComponent: RouteErrorPage });
    const failing = createRoute({
      getParentRoute: () => root,
      path: "/",
      component: function Failing(): never {
        throw new Error("render exploded");
      },
    });
    const router = createRouter({
      routeTree: root.addChildren([failing]),
      history: createMemoryHistory({ initialEntries: ["/"] }),
    });
    render(<RouterProvider router={router} />);
    expect(await screen.findByRole("heading", { name: "Something went wrong" })).toBeInTheDocument();
    expect(screen.queryByText(/render exploded/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
