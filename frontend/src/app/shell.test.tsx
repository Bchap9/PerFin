// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
  type AnyRoute,
} from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApiError } from "../lib/api/client";
import { ErrorBoundary } from "./ErrorBoundary";
import { NAV_ITEMS } from "./nav";
import { RouteErrorPage } from "./pages";
import { createAppRouter } from "./router";
import { normalizedPath, shellRoutes } from "./shellRoutes";

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createAppRouter({ queryClient }, createMemoryHistory({ initialEntries: [path] }));
  render(<RouterProvider router={router} />);
  return router;
}

describe("app shell", () => {
  it("shows every navigation section and marks the current one", async () => {
    renderAt("/tax");
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual([
      "Dashboard",
      "Accounts",
      "Transactions",
      "Budgets",
      "Goals",
      "Rules",
      "Investments",
      "Loans",
      "Assets",
      "Net worth",
      "Calendar",
      "Documents",
      "Review",
      "Tax",
      "Settings",
    ]);
    expect(within(nav).getByRole("link", { name: "Tax" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("aria-current");
    expect(await screen.findByRole("heading", { level: 1, name: "Tax" })).toBeInTheDocument();
    // Every section is a real feature now (Phases 5+6 built the last ones).
    expect(screen.queryByText("Not built yet")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });

  it("navigates between sections", async () => {
    const user = userEvent.setup();
    renderAt("/loans");
    expect(await screen.findByRole("heading", { level: 1, name: "Loans" })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Net worth" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Net worth" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Net worth" })).toHaveAttribute("aria-current", "page");
  });

  it("toggles the mobile navigation", async () => {
    const user = userEvent.setup();
    renderAt("/tax");
    const toggle = await screen.findByRole("button", { name: "Open navigation" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "app-sidebar");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Close navigation" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("navigation", { name: "Main" })).toHaveClass("app-sidebar--open");
  });

  it("closes the mobile navigation on Escape, on a tap outside it and when a link is chosen", async () => {
    const user = userEvent.setup();
    renderAt("/tax");
    const open = async () => {
      await user.click(await screen.findByRole("button", { name: "Open navigation" }));
      expect(screen.getByRole("navigation", { name: "Main" })).toHaveClass("app-sidebar--open");
    };
    const closed = () => {
      expect(screen.getByRole("navigation", { name: "Main" })).not.toHaveClass("app-sidebar--open");
      expect(screen.getByRole("button", { name: "Open navigation" })).toHaveAttribute("aria-expanded", "false");
    };
    await open();
    await user.keyboard("{Escape}");
    closed();

    await open();
    const backdrop = document.querySelector(".app-nav-backdrop");
    if (backdrop === null) {
      throw new Error("the drawer backdrop is missing");
    }
    await user.click(backdrop);
    closed();
    expect(document.querySelector(".app-nav-backdrop")).toBeNull();

    await open();
    await user.click(screen.getByRole("link", { name: "Loans" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Loans" })).toBeInTheDocument();
    closed();
  });

  it("renders a 404 page for unknown paths", async () => {
    renderAt("/no/such/page");
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to dashboard" })).toHaveAttribute("href", "/");
  });

  it("renders the login placeholder without the navigation chrome", async () => {
    renderAt("/login");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
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
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
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

describe("shell routes", () => {
  it("provides a placeholder per nav item plus login", () => {
    const paths = shellRoutes({} as AnyRoute).map(normalizedPath);
    expect(paths).toEqual([...NAV_ITEMS.map((item) => item.to.replace(/^\//, "")), "login"]);
  });

  it("has a feature route for every navigation section (no placeholder left)", () => {
    const router = createAppRouter({ queryClient: new QueryClient() }, createMemoryHistory({ initialEntries: ["/"] }));
    const children = (router.routeTree.children ?? []) as AnyRoute[];
    const placeholderPaths = new Set(shellRoutes({} as AnyRoute).map(normalizedPath));
    const served = children.filter((route) => placeholderPaths.has(normalizedPath(route)));
    // Each nav path (and login) appears once; none of them is the shell's placeholder.
    expect(served.map(normalizedPath).sort()).toEqual([...placeholderPaths].sort());
    for (const route of served) {
      expect((route.options as { component?: { name?: string } }).component?.name).not.toBe("Placeholder");
    }
  });

  it("normalises paths for comparison", () => {
    const parent = {} as AnyRoute;
    const route = createRoute({ getParentRoute: () => parent, path: "/accounts/" });
    expect(normalizedPath(route as unknown as AnyRoute)).toBe("accounts");
  });

  it("lets a feature's route replace the placeholder (the dashboard serves /)", async () => {
    renderAt("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("Not built yet")).not.toBeInTheDocument();
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
