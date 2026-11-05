// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { CAT_DINING, CAT_RENT, money, ownerSession, referenceHandlers } from "../planning/testing";
import { barWidth, lineStatus, percentOf, steppedPeriod } from "./report";

const STAMP = "2026-11-01T10:00:00Z";

const BUDGET = {
  id: "b-1",
  name: "Household",
  period: "monthly",
  start_date: "2026-01-01",
  end_date: null,
  currency: "USD",
  rollover: false,
  created_at: STAMP,
  updated_at: STAMP,
  lines: [
    { id: "l-1", category_id: CAT_DINING, amount: money("200.00") },
    { id: "l-2", category_id: CAT_RENT, amount: money("1500.00") },
  ],
};

const line = (categoryId: string, name: string, v: { budgeted: string; actual: string; forecast: string; carried?: string }) => {
  const carried = v.carried ?? "0.00";
  const available = (Number(v.budgeted) + Number(carried)).toFixed(2);
  return {
    category_id: categoryId,
    name,
    kind: "expense",
    budgeted: money(v.budgeted),
    carried_over: money(carried),
    available: money(available),
    actual: money(v.actual),
    forecast: money(v.forecast),
    remaining: money((Number(available) - Number(v.actual)).toFixed(2)),
  };
};

const totals = {
  budgeted: money("1700.00"),
  carried_over: money("0.00"),
  available: money("1700.00"),
  actual: money("1650.00"),
  forecast: money("1900.00"),
  remaining: money("50.00"),
};

const REPORT = {
  budget_id: "b-1",
  name: "Household",
  currency: "USD",
  as_of: "2026-11-15",
  include_pending: false,
  period: { start: "2026-11-01", end: "2026-11-30", state: "current" },
  lines: [
    line(CAT_DINING, "Dining", { budgeted: "200.00", actual: "250.00", forecast: "300.00" }),
    line(CAT_RENT, "Rent", { budgeted: "1500.00", actual: "1400.00", forecast: "1600.00" }),
    line("c-3", "Fun", { budgeted: "100.00", actual: "20.00", forecast: "40.00" }),
  ],
  expense_totals: totals,
  income_totals: { ...totals, budgeted: money("0.00"), actual: money("0.00"), forecast: money("0.00"), available: money("0.00"), remaining: money("0.00") },
  unbudgeted: [],
  unbudgeted_total: money("35.00"),
  unconverted: [],
};

describe("budget report helpers", () => {
  it("flags lines that are over or forecast to go over", () => {
    expect(lineStatus({ actual: { amount: "250" }, available: { amount: "200" }, forecast: { amount: "300" }, remaining: { amount: "-50" } })).toBe("over");
    expect(lineStatus({ actual: { amount: "100" }, available: { amount: "200" }, forecast: { amount: "300" }, remaining: { amount: "100" } })).toBe("forecast-over");
    expect(lineStatus({ actual: { amount: "10" }, available: { amount: "200" }, forecast: { amount: "40" }, remaining: { amount: "190" } })).toBe("ok");
    // Income below target is not overspending.
    expect(lineStatus({ actual: { amount: "0" }, available: { amount: "200" }, forecast: { amount: "0" }, remaining: { amount: "-200" } }, "income")).toBe("ok");
  });

  it("computes percentages and bar widths without floats", () => {
    expect(percentOf("250.00", "200.00")).toBe(125);
    expect(percentOf("1", "3")).toBe(33.3);
    expect(percentOf("1", "0")).toBeNull();
    expect(barWidth("250", "200")).toBe(100);
    expect(barWidth("-5", "200")).toBe(0);
    expect(barWidth("5", "0")).toBe(100);
    expect(barWidth("0", "0")).toBe(0);
  });

  it("steps by the budget's period", () => {
    const add = (iso: string, n: number) => `${iso}+${String(n)}`;
    expect(steppedPeriod("2026-11-01", "monthly", -1, add)).toBe("2026-11-01+-1");
    expect(steppedPeriod("2026-01-01", "annual", 1, add)).toBe("2026-01-01+12");
    expect(steppedPeriod("2026-01-01", "custom", 1, add)).toBeNull();
  });
});

describe("budget page", () => {
  function budgetApi() {
    return mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/budgets/:id": () => json(BUDGET),
      "GET /api/v1/budgets/:id/report": () => json(REPORT),
      "PATCH /api/v1/budgets/:id": () => json(BUDGET),
    });
  }

  it("shows budgeted, actual, forecast and remaining per category with a status", async () => {
    const api = budgetApi();
    renderApp("/budgets/b-1");
    const table = await screen.findByRole("region", { name: "Budget report by category" });
    const rows = within(table).getAllByRole("row");
    const dining = rows.find((row) => within(row).queryByText("Dining") !== null);
    expect(dining).toBeDefined();
    if (dining === undefined) {
      return;
    }
    expect(within(dining).getByText("$200.00")).toBeInTheDocument();
    expect(within(dining).getByText("$250.00")).toBeInTheDocument();
    expect(within(dining).getByText("$300.00")).toBeInTheDocument();
    expect(within(dining).getByText("-$50.00")).toBeInTheDocument();
    expect(within(dining).getByText("Over budget")).toBeInTheDocument();
    const rent = rows.find((row) => within(row).queryByText("Rent") !== null);
    expect(rent === undefined ? null : within(rent).getByText("Forecast over")).toBeInTheDocument();
    const fun = rows.find((row) => within(row).queryByText("Fun") !== null);
    expect(fun === undefined ? null : within(fun).getByText("On track")).toBeInTheDocument();
    expect(screen.getByText("November 2026")).toBeInTheDocument();
    expect(within(table).getByText("Spending outside the budget")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/budgets/b-1/report")[0]?.search).toBe("");
  });

  it("steps to the previous period and can include pending transactions", async () => {
    const api = budgetApi();
    renderApp("/budgets/b-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Previous period" }));
    await vi.waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/budgets/b-1/report").some((call) => call.search.includes("period=2026-10-01"))).toBe(true);
    });
    await user.click(screen.getByRole("checkbox", { name: "Include transactions awaiting review" }));
    await vi.waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/budgets/b-1/report").some((call) => call.search.includes("include_pending=true"))).toBe(true);
    });
  });

  it("edits the lines and sends amounts in the budget's currency", async () => {
    const api = budgetApi();
    renderApp("/budgets/b-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit budget" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit budget" });
    const amount = within(dialog).getByLabelText("Amount 1");
    await user.clear(amount);
    await user.type(amount, "225.50");
    await user.click(within(dialog).getByRole("button", { name: "Remove line 2" }));
    await user.click(within(dialog).getByRole("button", { name: "Save budget" }));
    await vi.waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/budgets/b-1")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/budgets/b-1")[0]?.body).toEqual({
      name: "Household",
      rollover: false,
      lines: [{ category_id: CAT_DINING, amount: { amount: "225.50", currency: "USD" } }],
    });
  });

  it("refuses a bad amount before calling the server", async () => {
    const api = budgetApi();
    renderApp("/budgets/b-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit budget" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit budget" });
    const amount = within(dialog).getByLabelText("Amount 1");
    await user.clear(amount);
    await user.type(amount, "abc");
    await user.click(within(dialog).getByRole("button", { name: "Save budget" }));
    expect(await within(dialog).findByText(/Amounts must be numbers/)).toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/budgets/b-1")).toHaveLength(0);
  });
});

describe("budget list", () => {
  it("creates a budget in the household currency and opens it", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/budgets": () => json({ items: [], next_cursor: null }),
      "POST /api/v1/budgets": () => json(BUDGET, 201),
      "GET /api/v1/budgets/:id": () => json(BUDGET),
      "GET /api/v1/budgets/:id/report": () => json(REPORT),
    });
    renderApp("/budgets");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New budget" }));
    const dialog = await screen.findByRole("dialog", { name: "New budget" });
    await user.click(within(dialog).getByRole("button", { name: "Create budget" }));
    expect(await within(dialog).findByText("Enter a name.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Name/), "Household");
    await user.click(within(dialog).getByRole("button", { name: "Create budget" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/budgets")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/budgets")[0]?.body).toMatchObject({ name: "Household", period: "monthly", currency: "USD", rollover: false });
    expect(await screen.findByRole("heading", { level: 1, name: "Household" })).toBeInTheDocument();
  });
});
