// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";

import { json, mockApi, problem, renderApp } from "../features/auth/testing";
import { money, ownerSession, referenceHandlers } from "../features/planning/testing";
import { account, page } from "../features/transactions/fixtures";
import { REDIRECTS, type Redirect } from "./routeMap";

const PARAMS = new Map([
  ["$documentId", "d-1"],
  ["$budgetId", "b-1"],
  ["$id", "s-1"],
  ["$year", "2025"],
  ["$ruleId", "r-1"],
  ["$loanId", "l-1"],
  ["$assetId", "x-1"],
]);

function fill(path: string): string {
  return path
    .split("/")
    .map((part) => PARAMS.get(part) ?? part)
    .join("/");
}

/** The heading the target page shows, so the test knows the right page rendered. */
const HEADINGS = new Map<string, RegExp>([
  ["/inbox", /^Inbox$/],
  ["/inbox/documents", /^Document library$/],
  ["/inbox/documents/$documentId", /^Document$/],
  ["/inbox/documents/$documentId/mapping", /^Map columns$/],
  ["/settings/learned", /^What PerFin has learned$/],
  ["/accounts/investments", /^Investments$/],
  ["/accounts?group=loans", /^Loans$/],
  ["/accounts?group=property", /^Property and other$/],
  ["/plan/subscriptions", /^Subscriptions$/],
  ["/plan/subscriptions/$id", /^Subscriptions$/],
  ["/plan/budgets", /^Budgets$/],
  ["/plan/budgets/$budgetId", /^Budget$/],
  ["/plan/goals", /^Goals$/],
  ["/plan/upcoming", /^Upcoming$/],
  ["/reports/net-worth", /^Net worth$/],
  ["/reports/tax", /^Tax$/],
  ["/reports/tax/$year", /2025/],
  ["/settings/categories?tab=tax", /^Schedule hints$/],
  ["/settings/rules", /^Rules$/],
  ["/settings/rules/new", /^New rule$/],
  ["/settings/rules/$ruleId", /rule/i],
]);

function headingFor(row: Redirect): RegExp {
  const key = row.search?.["group"] !== undefined || row.search?.["tab"] !== undefined
    ? `${row.to}?${new URLSearchParams(row.search).toString()}`
    : row.to;
  const heading = HEADINGS.get(key);
  if (heading === undefined) {
    throw new Error(`no heading listed for ${key}`);
  }
  return heading;
}

const LOAN = {
  id: "l-1",
  account_id: "a-9",
  principal_original: money("150000.00"),
  rate: "0.05",
  rate_type: "fixed",
  term_months: 360,
  origination_date: "2024-01-15",
  payment_amount: money("805.23"),
  payment_day: 15,
  escrow_amount: null,
  lender: "Example Lender",
  maturity_date: "2054-01-15",
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-01T10:00:00Z",
  projection: {
    as_of: "2026-11-03",
    balance: money("140000.00"),
    balance_source: "scheduled",
    rate: "0.05",
    scheduled_payment: money("805.23"),
    next_due_date: "2026-11-15",
    payoff_date: "2054-01-15",
    remaining_payments: 327,
    remaining_interest: money("120000.00"),
  },
};

const ASSET = {
  id: "x-1",
  account_id: "a-8",
  name: "Example cabin",
  kind: "property",
  description: null,
  valuation_method: "appraisal",
  current_value: null,
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-01T10:00:00Z",
};

const MORTGAGE = account("a-9", "Example mortgage", { type: "mortgage" });
const CABIN = account("a-8", "Example cabin", { type: "manual_asset" });

describe("redirects from every old address", () => {
  it.each(REDIRECTS.filter((row) => row.data === undefined).map((row) => [row.from, row] as const))(
    "%s",
    async (_from, row) => {
      mockApi({ ...ownerSession(), ...referenceHandlers() });
      const { router } = renderApp(`${fill(row.from)}?probe=1#here`);
      await waitFor(() => {
        expect(router.state.location.pathname).toBe(fill(row.to));
      });
      const search = new URLSearchParams(router.state.location.searchStr);
      expect(search.get("probe")).toBe("1");
      for (const [key, value] of Object.entries(row.search ?? {})) {
        expect(search.get(key)).toBe(value);
      }
      expect(router.state.location.hash).toBe("here");
      expect((await screen.findAllByRole("heading", { name: headingFor(row) })).length).toBeGreaterThan(0);
    },
  );

  it("/loans/l-1 goes to the loan's account with the schedule tab", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/loans/:id": () => json(LOAN),
      "GET /api/v1/loans": () => json({ items: [LOAN], next_cursor: null }),
      // The account hub reads the account the loan belongs to.
      "GET /api/v1/accounts": page([MORTGAGE]),
      "GET /api/v1/accounts/:id": () => json(MORTGAGE),
    });
    const { router } = renderApp("/loans/l-1?probe=1#here");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/accounts/a-9");
    });
    const search = new URLSearchParams(router.state.location.searchStr);
    expect(search.get("tab")).toBe("schedule");
    expect(search.get("probe")).toBe("1");
    expect(router.state.location.hash).toBe("here");
    expect(await screen.findByRole("button", { name: "Delete loan" })).toBeInTheDocument();
  });

  it("/assets/x-1 goes to the asset's account with the valuations tab", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/assets/:id": () => json(ASSET),
      "GET /api/v1/assets": () => json({ items: [ASSET], next_cursor: null }),
      // The account hub reads the account the asset belongs to; its h1 is the account's name.
      "GET /api/v1/accounts": page([CABIN]),
      "GET /api/v1/accounts/:id": () => json(CABIN),
    });
    const { router } = renderApp("/assets/x-1");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/accounts/a-8");
    });
    expect(new URLSearchParams(router.state.location.searchStr).get("tab")).toBe("valuations");
    expect(await screen.findByRole("heading", { name: "Example cabin" })).toBeInTheDocument();
  });

  it("a missing loan says so and links to Accounts", async () => {
    mockApi({ ...ownerSession(), "GET /api/v1/loans/:id": () => problem(404, "not-found") });
    const { router } = renderApp("/loans/gone");
    expect(await screen.findByText("This loan no longer exists.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Accounts" })).toHaveAttribute("href", "/accounts");
    expect(router.state.location.pathname).toBe("/loans/gone");
  });

  it("a missing asset says so too", async () => {
    mockApi({ ...ownerSession(), "GET /api/v1/assets/:id": () => problem(404, "not-found") });
    renderApp("/assets/gone");
    expect(await screen.findByText("This asset no longer exists.")).toBeInTheDocument();
  });

  it("/plan opens subscriptions", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { router } = renderApp("/plan");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/plan/subscriptions");
    });
    expect(await screen.findByRole("heading", { name: "Subscriptions" })).toBeInTheDocument();
  });

  it("/review?document=d-1 keeps the document", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { router } = renderApp("/review?document=d-1");
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/inbox");
    });
    expect(new URLSearchParams(router.state.location.searchStr).get("document")).toBe("d-1");
  });

  it("/reports keeps its tab and period", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { router } = renderApp("/reports?tab=income&period=last-month");
    expect((await screen.findAllByRole("heading", { name: "Reports" })).length).toBeGreaterThan(0);
    expect(router.state.location.pathname).toBe("/reports");
    const search = new URLSearchParams(router.state.location.searchStr);
    expect(search.get("tab")).toBe("income");
    expect(search.get("period")).toBe("last-month");
  });
});
