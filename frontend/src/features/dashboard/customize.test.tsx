// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp, type Handler } from "../auth/testing";
import { money, ownerSession, referenceHandlers } from "../planning/testing";

const PREFS = { display_name: "Alice", locale: "en-US", theme: "system", timezone: "UTC" };

const EMPTY: Record<string, Handler> = {
  "GET /api/v1/reports/cashflow": () => json({ currency: "USD", date_from: "2026-11-01", date_to: "2026-11-30", inflow: money("0"), outflow: money("0"), net: money("0"), months: [], unconverted: [] }),
  "GET /api/v1/networth/latest": () =>
    json({ currency: "USD", as_of: "2026-11-04", assets: money("5000.00"), liabilities: money("-1200.00"), net: money("3800.00"), accounts: [], by_class: {}, by_type: {}, unconverted: [], unpriced_security_ids: [], scope: "household", last_snapshot_date: null }),
  "GET /api/v1/goals": () =>
    json({
      items: [
        {
          id: "g-1",
          name: "Emergency fund",
          target: money("10000.00"),
          target_date: null,
          achieved_at: null,
          archived: false,
          account_ids: [],
          progress: { contributed: money("2500.00"), percent: "25", remaining: money("7500.00"), on_track: null, projected_completion: null, monthly_rate: null, accounts_balance: null, as_of: "2026-11-04" },
        },
      ],
      next_cursor: null,
    }),
  "GET /api/v1/cashflow/calendar": () =>
    json({
      currency: "USD",
      date_from: "2026-11-04",
      date_to: "2026-12-04",
      opening_balance: money("0"),
      bills: money("-100"),
      income: money("0"),
      net: money("-100"),
      unconverted: [],
      days: [
        { on: "2026-11-10", balance: money("0"), bills: money("-100"), income: money("0"), net: money("-100"), events: [{ schedule_id: "s-1", account_id: "a-1", name: "Electric company", kind: "bill", amount: money("-100.00"), amount_base: null, amount_min: money("-100.00"), amount_max: money("-100.00") }] },
      ],
    }),
  "GET /api/v1/investments/holdings": () =>
    json({
      as_of: "2026-11-04",
      base_currency: "USD",
      market_value: money("1250.00"),
      cost_basis: money("1000.00"),
      unrealized: { total: money("250.00"), long_term: money("250.00"), short_term: money("0") },
      holdings: [{ id: "h-1" }],
      accounts: [],
      allocation: { asset_class: [{ key: "equity", value: money("1250.00"), weight: "1" }] },
      missing_rates: [],
      unpriced_security_ids: [],
    }),
  "GET /api/v1/loans": () =>
    json({
      items: [
        {
          id: "l-1",
          account_id: "a-1",
          lender: "Synthetic Bank",
          projection: { balance: money("-90000.00"), next_due_date: "2026-12-01" },
        },
      ],
      next_cursor: null,
    }),
};

function setup(prefs: Record<string, unknown>, extra: Record<string, Handler> = {}) {
  let stored = prefs;
  const api = mockApi({
    ...ownerSession(),
    ...referenceHandlers(),
    ...EMPTY,
    "GET /api/v1/settings/user": () => json({ ...PREFS, ...stored }),
    "PATCH /api/v1/settings/user": (call) => {
      stored = { ...stored, ...(call.body as object) };
      return json({ ...PREFS, ...stored });
    },
    ...extra,
  });
  return api;
}

const lastLayout = (api: ReturnType<typeof setup>) => {
  const calls = api.callsTo("PATCH", "/api/v1/settings/user");
  return (calls.at(-1)?.body as { dashboard_layout: { widget: string; size: string }[] }).dashboard_layout;
};

describe("configurable dashboard", () => {
  it("shows the saved layout, in order, with only those widgets", async () => {
    setup({ dashboard_layout: [{ widget: "goals", size: "medium" }, { widget: "net_worth", size: "small" }] });
    renderApp("/");
    const goals = await screen.findByRole("region", { name: "Goals" });
    const netWorth = screen.getByRole("region", { name: "Net worth" });
    expect(goals.compareDocumentPosition(netWorth) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByRole("region", { name: /Cash flow/ })).not.toBeInTheDocument();
    expect(await within(goals).findByText("Emergency fund")).toBeInTheDocument();
    expect(await within(netWorth).findByText("$3,800.00")).toBeInTheDocument();
  });

  it("shows the new widgets: bills, holdings and loans", async () => {
    setup({
      dashboard_layout: [
        { widget: "upcoming_bills", size: "medium" },
        { widget: "holdings", size: "medium" },
        { widget: "loans", size: "medium" },
      ],
    });
    renderApp("/");
    const bills = await screen.findByRole("region", { name: "Upcoming bills" });
    expect(await within(bills).findByText("Electric company")).toBeInTheDocument();
    const holdings = screen.getByRole("region", { name: "Holdings and allocation" });
    expect((await within(holdings).findAllByText("$1,250.00")).length).toBeGreaterThan(0);
    expect(await within(holdings).findByRole("list", { name: "Allocation by asset class" })).toBeInTheDocument();
    const loans = screen.getByRole("region", { name: "Loans" });
    expect(await within(loans).findByRole("link", { name: "Synthetic Bank" })).toBeInTheDocument();
  });

  it("shows an empty dashboard as empty, not as the default", async () => {
    setup({ dashboard_layout: [] });
    renderApp("/");
    expect(await screen.findByText(/The dashboard is empty/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Cash flow/ })).not.toBeInTheDocument();
  });

  it("adds, resizes, reorders and removes widgets, saving each change", async () => {
    const user = userEvent.setup();
    const api = setup({ dashboard_layout: [{ widget: "goals", size: "medium" }, { widget: "loans", size: "medium" }] });
    renderApp("/");
    await screen.findByRole("region", { name: "Goals" });
    await user.click(screen.getByRole("button", { name: "Customize" }));

    await user.click(screen.getByRole("button", { name: "Add Net worth" }));
    await waitFor(() => {
      expect(lastLayout(api)).toEqual([
        { widget: "goals", size: "medium" },
        { widget: "loans", size: "medium" },
        { widget: "net_worth", size: "medium" },
      ]);
    });
    expect(await screen.findByRole("region", { name: "Net worth" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add Net worth" })).not.toBeInTheDocument();

    await user.selectOptions(screen.getByRole("combobox", { name: "Size of Loans" }), "large");
    await waitFor(() => {
      expect(lastLayout(api)[1]).toEqual({ widget: "loans", size: "large" });
    });

    await user.click(screen.getByRole("button", { name: "Move Loans earlier" }));
    await waitFor(() => {
      expect(lastLayout(api).map((e) => e.widget)).toEqual(["loans", "goals", "net_worth"]);
    });
    expect(screen.getByRole("button", { name: "Move Loans earlier" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Remove Goals" }));
    await waitFor(() => {
      expect(lastLayout(api).map((e) => e.widget)).toEqual(["loans", "net_worth"]);
    });
    expect(screen.queryByRole("region", { name: "Goals" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add Goals" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByRole("button", { name: "Add Goals" })).not.toBeInTheDocument();
  });

  it("resets to the default layout", async () => {
    const user = userEvent.setup();
    const api = setup({ dashboard_layout: [] });
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Customize" }));
    await user.click(screen.getByRole("button", { name: "Reset to default" }));
    await waitFor(() => {
      expect(lastLayout(api).length).toBeGreaterThan(5);
    });
    expect(await screen.findByRole("region", { name: /Cash flow/ })).toBeInTheDocument();
  });

  it("rolls back and tells the person when saving fails", async () => {
    const user = userEvent.setup();
    setup(
      { dashboard_layout: [{ widget: "goals", size: "medium" }] },
      { "PATCH /api/v1/settings/user": () => json({ type: "urn:perfin:problem:server-error", title: "Boom", status: 500 }, 500) },
    );
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Customize" }));
    await user.click(screen.getByRole("button", { name: "Remove Goals" }));
    expect(await screen.findByText("Could not save the dashboard layout")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Goals" })).toBeInTheDocument();
  });

  it("does not offer customising when the preferences cannot be read", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers(), ...EMPTY, "GET /api/v1/settings/user": () => json({ status: 500, title: "x", type: "urn:perfin:problem:server-error" }, 500) });
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: /Cash flow/ })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Customize" })).toBeDisabled();
    });
  });
});
