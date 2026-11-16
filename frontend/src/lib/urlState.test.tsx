// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen } from "@testing-library/react";

import { useSearchParam, useTabParam } from "./urlState";

const TABS = ["holdings", "performance", "activity"] as const;

function renderAt(path: string) {
  const seen: { tab: (typeof TABS)[number]; setTab: (id: (typeof TABS)[number]) => void; q: string; setQ: (value: string) => void }[] = [];
  function Probe() {
    const [tab, setTab] = useTabParam(TABS, "holdings");
    const [q, setQ] = useSearchParam("q", "");
    seen.push({ tab, setTab, q, setQ });
    return (
      <p>
        tab={tab} q={q}
      </p>
    );
  }
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <Probe />
        <Outlet />
      </>
    ),
  });
  const anyRoute = createRoute({ getParentRoute: () => rootRoute, path: "$", component: () => null });
  const router = createRouter({
    routeTree: rootRoute.addChildren([anyRoute]),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(<RouterProvider router={router} />);
  const latest = () => {
    const last = seen.at(-1);
    if (last === undefined) throw new Error("not rendered");
    return last;
  };
  return { router, latest };
}

describe("useTabParam", () => {
  it("reads the value", async () => {
    renderAt("/accounts/a-1?tab=performance");
    expect(await screen.findByText("tab=performance q=")).toBeInTheDocument();
  });

  it("ignores an unknown value", async () => {
    renderAt("/accounts/a-1?tab=bogus");
    expect(await screen.findByText("tab=holdings q=")).toBeInTheDocument();
  });

  it("writes with replace and leaves the default out", async () => {
    const { router, latest } = renderAt("/accounts/a-1?keep=1");
    await screen.findByText("tab=holdings q=");
    const before = router.history.length;
    act(() => {
      latest().setTab("activity");
    });
    expect(await screen.findByText("tab=activity q=")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ keep: 1, tab: "activity" });
    expect(router.history.length).toBe(before);

    act(() => {
      latest().setTab("holdings");
    });
    expect(await screen.findByText("tab=holdings q=")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ keep: 1 });
    expect(router.state.location.pathname).toBe("/accounts/a-1");
    expect(router.history.length).toBe(before);
  });
});

describe("useSearchParam", () => {
  it("reads a value the router parsed as a number or boolean as text", async () => {
    renderAt("/transactions?q=2024");
    expect(await screen.findByText("tab=holdings q=2024")).toBeInTheDocument();
  });

  it("writes with replace and leaves the fallback out", async () => {
    const { router, latest } = renderAt("/transactions?tab=activity");
    await screen.findByText("tab=activity q=");
    act(() => {
      latest().setQ("coffee");
    });
    expect(await screen.findByText("tab=activity q=coffee")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ tab: "activity", q: "coffee" });
    act(() => {
      latest().setQ("");
    });
    expect(await screen.findByText("tab=activity q=")).toBeInTheDocument();
    expect(router.state.location.search).toEqual({ tab: "activity" });
  });
});
