// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { json, mockApi, renderApp } from "../auth/testing";
import { money, ownerSession } from "../planning/testing";

const history = (scope: string) => ({
  currency: "USD",
  range: "1y",
  granularity: "month",
  scope,
  date_from: "2026-01-31",
  date_to: "2026-02-28",
  change: money("1100.10"),
  points: [
    { as_of: "2026-01-31", assets: money("10000.10"), liabilities: money("4000.00"), net: money("6000.10"), source: "snapshot" },
    { as_of: "2026-02-28", assets: money("11000.20"), liabilities: money("3900.00"), net: money("7100.20"), source: "computed" },
  ],
});

const latest = (scope: string) => ({
  as_of: "2026-11-03",
  currency: "USD",
  scope,
  assets: money("11000.20"),
  liabilities: money("3900.00"),
  net: money("7100.20"),
  by_class: { assets: { cash: money("1000.20"), investments: money("10000.00") }, liabilities: { mortgages: money("3900.00") } },
  by_type: {},
  unconverted: [],
  unpriced_security_ids: [],
  last_snapshot_date: "2026-11-02",
  accounts: [
    { account_id: "a1", name: "Checking", type: "checking", account_class: "cash", side: "asset", amount: money("1000.20"), amount_base: money("1000.20") },
    { account_id: "a2", name: "Index fund", type: "brokerage", account_class: "investments", side: "asset", amount: money("10000.00"), amount_base: money("10000.00") },
    { account_id: "a3", name: "Home loan", type: "mortgage", account_class: "mortgages", side: "liability", amount: money("-3900.00"), amount_base: money("-3900.00") },
  ],
});

const projection = {
  as_of: "2026-11-03",
  currency: "USD",
  scope: "household",
  months: 12,
  basis_months: 12,
  current: money("7100.20"),
  trend: { slope_per_month: money("100.00"), points: [{ on: "2026-12-03", net: money("7200.20") }] },
  savings: { average_monthly_savings: money("250.00"), savings_rate: "0.1", points: [{ on: "2026-12-03", net: money("7350.20") }] },
  unconverted: [],
};

describe("net worth page", () => {
  it("shows totals, the change, the breakdown and the projection", async () => {
    mockApi({
      ...ownerSession(),
      "GET /api/v1/networth": () => json(history("household")),
      "GET /api/v1/networth/latest": () => json(latest("household")),
      "GET /api/v1/networth/projection": () => json(projection),
    });
    renderApp("/reports/net-worth");
    expect(await screen.findByRole("img", { name: "Net worth over time, 2 points" })).toBeInTheDocument();
    expect((await screen.findAllByText("$7,100.20")).length).toBeGreaterThan(0);
    expect(screen.getByText("+$1,100.10")).toBeInTheDocument();
    const classes = screen.getByRole("table", { name: /^Assets by class/ });
    expect(within(classes).getByText("Investments")).toBeInTheDocument();
    expect(await screen.findByText("10.00%")).toBeInTheDocument();
    expect(screen.queryByText("Your accounts only")).not.toBeInTheDocument();
  });

  it("says when the figures cover only the accounts the person can see, and refetches on a new range", async () => {
    const api = mockApi({
      ...ownerSession({ role: "member" }),
      "GET /api/v1/networth": () => json(history("visible_accounts")),
      "GET /api/v1/networth/latest": () => json(latest("visible_accounts")),
      "GET /api/v1/networth/projection": () => json({ ...projection, scope: "visible_accounts", trend: null }),
    });
    renderApp("/reports/net-worth");
    expect(await screen.findByText("Your accounts only")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "3 months" }));
    await vi.waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/networth").some((call) => call.search.includes("range=3m"))).toBe(true);
    });
    expect(await screen.findByText("Not enough history")).toBeInTheDocument();
  });

  it("every breakdown row links to its account", async () => {
    mockApi({
      ...ownerSession(),
      "GET /api/v1/networth": () => json(history("household")),
      "GET /api/v1/networth/latest": () => json(latest("household")),
      "GET /api/v1/networth/projection": () => json(projection),
    });
    renderApp("/reports/net-worth");
    const assets = await screen.findByRole("table", { name: /^Assets by account/ });
    expect(within(assets).getByRole("link", { name: "Checking" })).toHaveAttribute("href", "/accounts/a1");
    expect(within(assets).getByRole("link", { name: "Index fund" })).toHaveAttribute("href", "/accounts/a2");
    const liabilities = screen.getByRole("table", { name: /^Liabilities by account/ });
    expect(within(liabilities).getByRole("link", { name: "Home loan" })).toHaveAttribute("href", "/accounts/a3");
    expect(within(liabilities).getByText("$3,900.00")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    mockApi({
      ...ownerSession(),
      "GET /api/v1/networth": () => json(history("household")),
      "GET /api/v1/networth/latest": () => json(latest("household")),
      "GET /api/v1/networth/projection": () => json(projection),
    });
    const { container } = renderApp("/reports/net-worth");
    await screen.findByRole("img", { name: "Net worth over time, 2 points" });
    await screen.findByRole("img", { name: /Net worth projection/ });
    await expectNoAxeViolations(container);
  });
});
