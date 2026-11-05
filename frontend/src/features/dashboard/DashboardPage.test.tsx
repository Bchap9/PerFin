// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";

import { json, mockApi, renderApp } from "../auth/testing";
import { CAT_DINING, ACCOUNT_CHECKING, money, ownerSession, referenceHandlers, CATEGORIES } from "../planning/testing";
import type { Account } from "../planning/queries";
import { accountTotals, monthRange } from "./DashboardPage";

const STAMP = "2026-11-01T10:00:00Z";

describe("dashboard helpers", () => {
  it("covers the calendar month", () => {
    expect(monthRange("2026-02-17")).toEqual({ from: "2026-02-01", to: "2026-02-28" });
  });

  it("totals open accounts per currency, assets and liabilities apart", () => {
    const account = (patch: Partial<Account>): Account =>
      ({ id: "a", name: "A", is_asset: true, is_closed: false, include_in_net_worth: true, balance: money("0"), ...patch }) as Account;
    expect(
      accountTotals([
        account({ balance: money("100.10") }),
        account({ balance: money("50.05") }),
        account({ is_asset: false, balance: money("-20.00") }),
        account({ balance: money("999"), is_closed: true }),
        account({ balance: money("999"), include_in_net_worth: false }),
        account({ balance: money("5", "EUR") }),
      ]),
    ).toEqual([
      { currency: "EUR", assets: "5", liabilities: "0" },
      { currency: "USD", assets: "150.15", liabilities: "-20.00" },
    ]);
  });
});

describe("dashboard", () => {
  it("shows cash flow, spending, budget status, recent transactions, review and accounts", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/reports/cashflow": () =>
        json({ currency: "USD", date_from: "2026-11-01", date_to: "2026-11-30", include_pending: false, inflow: money("3000.00"), outflow: money("-1200.00"), net: money("1800.00"), months: [], unconverted: [] }),
      "GET /api/v1/reports/spending": () =>
        json({
          currency: "USD",
          date_from: "2026-11-01",
          date_to: "2026-11-30",
          depth: 1,
          flow: "spending",
          group_by: "category",
          include_pending: false,
          total: money("300.00"),
          unconverted: [],
          items: [
            { key: CAT_DINING, name: "Dining", amount: money("200.00") },
            { key: null, name: null, amount: money("100.00") },
          ],
        }),
      "GET /api/v1/budgets": () =>
        json({
          items: [{ id: "b-1", name: "Household", period: "monthly", start_date: "2026-01-01", end_date: null, currency: "USD", rollover: false, lines: [], created_at: STAMP, updated_at: STAMP }],
          next_cursor: null,
        }),
      "GET /api/v1/budgets/:id/report": () => {
        const t = { budgeted: money("500.00"), carried_over: money("0"), available: money("500.00"), actual: money("620.00"), forecast: money("700.00"), remaining: money("-120.00") };
        return json({
          budget_id: "b-1",
          name: "Household",
          currency: "USD",
          as_of: "2026-11-15",
          include_pending: false,
          period: { start: "2026-11-01", end: "2026-11-30", state: "current" },
          lines: [{ ...t, category_id: CAT_DINING, name: "Dining", kind: "expense" }],
          expense_totals: t,
          income_totals: t,
          unbudgeted: [],
          unbudgeted_total: money("0"),
          unconverted: [],
        });
      },
      "GET /api/v1/transactions": () =>
        json({
          next_cursor: null,
          items: [
            {
              id: "t-1",
              account_id: ACCOUNT_CHECKING,
              amount: money("-12.00"),
              posted_date: "2026-11-02",
              raw_payee: "CAFE UNO",
              memo: null,
              notes: null,
              category_id: CATEGORIES[1]?.id ?? null,
              payee_id: null,
              review_state: "none",
              status: "cleared",
              tag_ids: [],
              splits: [],
              transfer: null,
              confidence: null,
              effective_date: null,
              source_document_id: null,
              created_at: STAMP,
              updated_at: STAMP,
            },
          ],
        }),
      "GET /api/v1/review": () => json({ items: [{ id: "r-1" }, { id: "r-2" }, { id: "r-3" }], next_cursor: null }),
    });
    renderApp("/");

    expect(await screen.findByRole("heading", { level: 1, name: "Dashboard" })).toBeInTheDocument();
    const cashflow = await screen.findByRole("region", { name: /Cash flow/ });
    expect(await within(cashflow).findByText("$3,000.00")).toBeInTheDocument();
    expect(within(cashflow).getByText("-$1,200.00")).toBeInTheDocument();
    expect(within(cashflow).getByText("$1,800.00")).toBeInTheDocument();

    const spending = screen.getByRole("region", { name: "Spending by category" });
    expect(await within(spending).findByText("Dining")).toBeInTheDocument();
    expect(within(spending).getByText("Uncategorised")).toBeInTheDocument();

    const budgets = screen.getByRole("region", { name: "Budgets" });
    expect(await within(budgets).findByText("Over budget")).toBeInTheDocument();

    const review = screen.getByRole("region", { name: "Needs review" });
    expect(await within(review).findByText("3")).toBeInTheDocument();

    const accounts = screen.getByRole("region", { name: "Accounts" });
    expect((await within(accounts).findAllByText("$1,200.50")).length).toBeGreaterThan(0);

    const recent = screen.getByRole("region", { name: "Recent transactions" });
    expect(await within(recent).findByText("CAFE UNO")).toBeInTheDocument();
    expect(within(recent).getByText("Dining")).toBeInTheDocument();

    expect(api.callsTo("GET", "/api/v1/reports/cashflow")[0]?.search).toMatch(/date_from=\d{4}-\d{2}-01/);
    expect(api.callsTo("GET", "/api/v1/review")[0]?.search).toContain("state=open");
  });

  it("keeps the rest of the page when one card fails", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/reports/cashflow": () => json({ type: "urn:perfin:problem:server-error", title: "Boom", status: 500 }, 500),
      "GET /api/v1/reports/spending": () => json({ currency: "USD", items: [], total: money("0"), unconverted: [] }),
      "GET /api/v1/budgets": () => json({ items: [], next_cursor: null }),
      "GET /api/v1/transactions": () => json({ items: [], next_cursor: null }),
      "GET /api/v1/review": () => json({ items: [], next_cursor: null }),
    });
    renderApp("/");
    expect(await screen.findByText(/Could not load cash flow/)).toBeInTheDocument();
    expect(await screen.findByText("Nothing is waiting for you.")).toBeInTheDocument();
    expect(await screen.findByText("No transactions yet")).toBeInTheDocument();
  });
});
