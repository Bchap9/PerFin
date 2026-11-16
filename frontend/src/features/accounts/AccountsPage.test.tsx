// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { account, page } from "../transactions/fixtures";
import { groupAccounts } from "./api";

const ACCOUNTS = [
  account("a-1", "Everyday", {
    balance: { amount: "1200.50", currency: "USD" },
    balance_date: "2026-11-12",
  }),
  account("a-2", "Rainy day", {
    type: "savings",
    balance: { amount: "5000.00", currency: "USD" },
  }),
  account("a-3", "Visa", {
    type: "credit_card",
    balance: { amount: "-300.25", currency: "USD" },
  }),
  account("a-4", "Bills", { balance: { amount: "99.50", currency: "USD" } }),
  account("a-5", "Old", { is_closed: true, closed_on: "2026-01-01" }),
  account("a-6", "Example mortgage", {
    type: "mortgage",
    is_asset: false,
    balance: { amount: "-190000.00", currency: "USD" },
  }),
  account("a-7", "Lake cabin", {
    type: "manual_asset",
    asset_kind: "property",
    balance: { amount: "180000.00", currency: "USD" },
    balance_date: "2026-06-01",
  }),
];

const NET_WORTH: Record<string, Handler> = {
  "GET /api/v1/networth/latest": () =>
    json({
      as_of: "2026-11-16",
      currency: "USD",
      scope: "household",
      net: { amount: "-4000.25", currency: "USD" },
      assets: { amount: "186300.00", currency: "USD" },
      liabilities: { amount: "190300.25", currency: "USD" },
      by_class: {},
      by_type: {},
      accounts: [],
      last_snapshot_date: null,
      unconverted: [],
      unpriced_security_ids: [],
    }),
  "GET /api/v1/networth": () =>
    json({
      currency: "USD",
      range: "1m",
      granularity: "daily",
      scope: "household",
      date_from: "2025-11-30",
      date_to: "2026-11-16",
      points: [
        { as_of: "2026-09-30", net: { amount: "-3900.25", currency: "USD" } },
        { as_of: "2026-10-31", net: { amount: "-3750.25", currency: "USD" } },
        { as_of: "2026-11-16", net: { amount: "-4000.25", currency: "USD" } },
      ],
      change: { amount: "1200.00", currency: "USD" },
    }),
  "GET /api/v1/loans": () => json({ items: [], next_cursor: null }),
  "GET /api/v1/assets": () => json({ items: [], next_cursor: null }),
};

function handlers(
  role: "owner" | "member" | "viewer",
  extra: Record<string, Handler> = {},
): Record<string, Handler> {
  return {
    "GET /api/v1/auth/session": authedSession({ role }),
    "GET /api/v1/accounts": page(ACCOUNTS),
    ...NET_WORTH,
    ...extra,
  };
}

describe("account grouping (api)", () => {
  it("groups by type in a fixed order", () => {
    const groups = groupAccounts(ACCOUNTS.slice(0, 5));
    expect(groups.map((group) => group.type)).toEqual([
      "checking",
      "savings",
      "credit_card",
    ]);
    expect(groups[0]?.accounts.map((item) => item.name)).toEqual([
      "Bills",
      "Everyday",
      "Old",
    ]);
  });
});

describe("accounts page", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 10, 16, 9, 30));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("groups with totals and net worth on top", async () => {
    mockApi(handlers("owner"));
    renderApp("/accounts");
    const worth = await screen.findByRole("region", { name: "Net worth" });
    expect(await within(worth).findByText("-$4,000.25")).toBeInTheDocument();
    // The month-to-date change, worded and toned as on Home: money down is not red.
    const change = await within(worth).findByText("Down $250.00 this month");
    expect(change).toHaveClass("stat__change--neutral");
    expect(
      within(worth).getByRole("link", { name: "Net worth over time" }),
    ).toHaveAttribute("href", "/reports/net-worth");

    const cash = await screen.findByRole("region", { name: "Cash" });
    expect(within(cash).getByText("$6,300.00")).toBeInTheDocument();
    expect(within(cash).getByRole("link", { name: "Everyday" })).toHaveAttribute(
      "href",
      "/accounts/a-1",
    );
    expect(within(cash).getByText("$1,200.50")).toBeInTheDocument();
    expect(within(cash).getByText("Balance from Nov 12")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Cards and credit" }),
    ).toHaveTextContent("-$300.25");
    expect(screen.getByRole("region", { name: "Loans" })).toHaveTextContent(
      "Example mortgage",
    );
    const property = screen.getByRole("region", { name: "Property and other" });
    expect(within(property).getByText("Your estimate, June 2026")).toBeInTheDocument();
    expect(
      screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(["Cash", "Cards and credit", "Loans", "Property and other"]);
  });

  it("?group=loans focuses the loans group", async () => {
    mockApi(handlers("owner"));
    renderApp("/accounts?group=loans");
    const heading = await screen.findByRole("heading", { name: "Loans" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
  });

  it("?group= names an empty group and lands on its heading", async () => {
    mockApi(handlers("owner", { "GET /api/v1/accounts": page(ACCOUNTS.slice(0, 2)) }));
    renderApp("/accounts?group=property");
    const heading = await screen.findByRole("heading", { name: "Property and other" });
    await waitFor(() => {
      expect(heading).toHaveFocus();
    });
    expect(
      screen.getByRole("region", { name: "Property and other" }),
    ).toHaveTextContent("No open accounts of this kind yet.");
    expect(screen.queryByRole("heading", { name: "Loans" })).not.toBeInTheDocument();
  });

  it("closed accounts are folded", async () => {
    mockApi(handlers("owner"));
    renderApp("/accounts");
    const cash = await screen.findByRole("region", { name: "Cash" });
    expect(within(cash).queryByText("Old")).not.toBeInTheDocument();
    const summary = screen.getByText("Closed (1)");
    const details = summary.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(details as HTMLElement).getByRole("link", { name: "Old" })).toHaveAttribute(
      "href",
      "/accounts/a-5",
    );
    await userEvent.setup().click(summary);
    expect(details).toHaveAttribute("open");
  });

  it("shows a loan's terms and an asset's details on their rows, and offers to add them where missing", async () => {
    mockApi(
      handlers("owner", {
        "GET /api/v1/loans": () =>
          json({
            items: [
              {
                id: "l-1",
                account_id: "a-6",
                lender: "Fictional Bank",
                rate: "0.06125",
                projection: {
                  balance: { amount: "190000.00", currency: "USD" },
                  next_due_date: "2026-11-15",
                  payoff_date: "2054-01-15",
                },
              },
            ],
            next_cursor: null,
          }),
        "GET /api/v1/accounts": page([
          ...ACCOUNTS,
          account("a-8", "Car loan", { type: "loan", is_asset: false }),
        ]),
      }),
    );
    renderApp("/accounts");
    const loans = await screen.findByRole("region", { name: "Loans" });
    expect(
      await within(loans).findByText(/6\.125% · Fictional Bank · next due Nov 15, 2026 · payoff Jan 15, 2054/),
    ).toBeInTheDocument();
    expect(
      within(loans).getByRole("link", { name: "Schedule of Example mortgage" }),
    ).toHaveAttribute("href", "/accounts/a-6?tab=schedule");
    expect(
      within(loans).getByRole("button", { name: "Add loan terms for Car loan" }),
    ).toBeInTheDocument();
    const property = screen.getByRole("region", { name: "Property and other" });
    expect(
      within(property).getByRole("button", { name: "Add asset details for Lake cabin" }),
    ).toBeInTheDocument();
  });

  it("adds terms to a loan account that has none", async () => {
    const api = mockApi(
      handlers("owner", {
        "POST /api/v1/loans": () => json({ id: "l-9" }, 201),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Add loan terms for Example mortgage" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Loan terms for Example mortgage" });
    await user.type(within(dialog).getByLabelText(/^Original amount/), "200000");
    await user.type(within(dialog).getByLabelText(/^Yearly rate/), "6.125");
    await user.type(within(dialog).getByLabelText(/^Term/), "360");
    await user.click(within(dialog).getByRole("button", { name: "Add loan" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/loans")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/loans")[0]?.body).toMatchObject({
      account_id: "a-6",
      principal_original: { amount: "200000", currency: "USD" },
    });
  });

  it("adding a manual asset creates the asset too", async () => {
    const api = mockApi(
      handlers("member", {
        "POST /api/v1/accounts": () =>
          json(account("a-9", "Fishing boat", { type: "manual_asset" }), 201),
        "POST /api/v1/assets": () => json({ id: "x-9" }, 201),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add account" }));
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Fishing boat");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "manual_asset");
    expect(within(dialog).queryByLabelText("Opening balance")).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("What it is"), "vehicle");
    await user.selectOptions(within(dialog).getByLabelText("How you value it"), "appraisal");
    await user.type(within(dialog).getByLabelText(/^First value/), "18000.00");
    await user.clear(within(dialog).getByLabelText("Valued on"));
    await user.type(within(dialog).getByLabelText("Valued on"), "2026-11-01");
    await user.click(within(dialog).getByRole("button", { name: "Create account" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/assets")).toHaveLength(1);
    });
    const created = api.callsTo("POST", "/api/v1/accounts")[0]?.body;
    expect(created).toMatchObject({ name: "Fishing boat", type: "manual_asset", asset_kind: "vehicle" });
    expect(created).not.toHaveProperty("opening_balance");
    expect(api.callsTo("POST", "/api/v1/assets")[0]?.body).toEqual({
      account_id: "a-9",
      valuation_method: "appraisal",
      value: { amount: "18000.00", currency: "USD" },
      valued_on: "2026-11-01",
    });
    expect(await screen.findByText("Account created")).toBeInTheDocument();
  });

  it("says so when the asset could not be added after its account was", async () => {
    mockApi(
      handlers("member", {
        "POST /api/v1/accounts": () =>
          json(account("a-9", "Fishing boat", { type: "manual_asset" }), 201),
        "POST /api/v1/assets": () =>
          json({ type: "about:blank", title: "Nope", status: 422, detail: "Not today." }, 422),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add account" }));
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Fishing boat");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "manual_asset");
    await user.type(within(dialog).getByLabelText(/^First value/), "18000.00");
    await user.click(within(dialog).getByRole("button", { name: "Create account" }));
    expect(
      await screen.findByText(/The account was created, but its asset details were not saved/),
    ).toBeInTheDocument();
  });

  it("adding a mortgage opens the account with its terms in one request", async () => {
    const api = mockApi(
      handlers("member", {
        "POST /api/v1/loans": () => json({ id: "l-9", account_id: "a-9" }, 201),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add account" }));
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Home mortgage");
    await user.selectOptions(within(dialog).getByLabelText("Type"), "mortgage");
    expect(within(dialog).getByRole("checkbox", { name: "Record the loan terms now" })).toBeChecked();
    expect(within(dialog).queryByLabelText("Opening balance")).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Original amount/), "200000");
    await user.type(within(dialog).getByLabelText(/^Yearly rate/), "6.125");
    await user.type(within(dialog).getByLabelText(/^Term/), "360");
    await user.click(within(dialog).getByRole("button", { name: "Create account" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/loans")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/loans")[0]?.body).toMatchObject({
      account: { name: "Home mortgage", type: "mortgage" },
      principal_original: { amount: "200000", currency: "USD" },
      rate: "0.06125",
      term_months: 360,
    });
    expect(api.callsTo("POST", "/api/v1/accounts")).toHaveLength(0);
  });

  it("creates an account with a decimal-string opening balance", async () => {
    const api = mockApi(
      handlers("member", {
        "GET /api/v1/accounts": page([]),
        "POST /api/v1/accounts": () => json(account("a-9", "Fresh"), 201),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Add account" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Fresh");
    await user.type(within(dialog).getByLabelText("Opening balance"), "250.75");
    await user.click(
      within(dialog).getByRole("button", { name: "Create account" }),
    );
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/accounts")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/accounts")[0]?.body).toMatchObject({
      name: "Fresh",
      type: "checking",
      currency: "USD",
      opening_balance: { amount: "250.75", currency: "USD" },
    });
  });

  it("creates a retirement account of a chosen kind and shows the kind", async () => {
    const api = mockApi(
      handlers("member", {
        "GET /api/v1/accounts": page([
          account("a-1", "Example Roth", {
            type: "retirement",
            subtype: "roth_ira",
            subtype_label: "Roth IRA",
          }),
        ]),
        "GET /api/v1/accounts/subtypes": () =>
          json([
            { type: "checking", subtypes: [] },
            {
              type: "retirement",
              subtypes: [
                {
                  code: "roth_ira",
                  label: "Roth IRA",
                  tax_treatment: "tax_free",
                },
                {
                  code: "simple_ira",
                  label: "SIMPLE IRA",
                  tax_treatment: "tax_deferred",
                },
              ],
            },
          ]),
        "POST /api/v1/accounts": () => json(account("a-9", "Work plan"), 201),
      }),
    );
    renderApp("/accounts");
    const user = userEvent.setup();
    expect(
      await screen.findByText("· Roth IRA", { exact: false }),
    ).toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: "Add account" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    expect(within(dialog).queryByLabelText(/^Kind/)).not.toBeInTheDocument(); // checking has none here
    await user.type(within(dialog).getByLabelText(/^Name/), "Work plan");
    await user.selectOptions(
      within(dialog).getByLabelText("Type"),
      "retirement",
    );
    await user.selectOptions(
      await within(dialog).findByLabelText(/^Kind/),
      "simple_ira",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Create account" }),
    );
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/accounts")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/accounts")[0]?.body).toMatchObject({
      type: "retirement",
      subtype: "simple_ira",
    });
  });

  it("says so when the loans or assets could not be loaded, and retries", async () => {
    const state = { fail: true };
    const api = mockApi(
      handlers("owner", {
        "GET /api/v1/loans": () =>
          state.fail ? problem(500, "server-error") : json({ items: [], next_cursor: null }),
        "GET /api/v1/assets": () => problem(500, "server-error"),
      }),
    );
    renderApp("/accounts");
    const loansAlert = await screen.findByText(/^The loan terms could not be loaded/);
    expect(loansAlert.closest("[role=alert]")).not.toBeNull();
    expect(
      await screen.findByText(/^The asset details could not be loaded/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Add loan terms/ }),
    ).not.toBeInTheDocument();
    state.fail = false;
    await userEvent
      .setup()
      .click(within(loansAlert).getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(
        screen.queryByText(/^The loan terms could not be loaded/),
      ).not.toBeInTheDocument();
    });
    expect(api.callsTo("GET", "/api/v1/loans").length).toBeGreaterThan(1);
  });

  it("a member gets no Add loan terms or asset details on an account they cannot edit", async () => {
    mockApi(
      handlers("member", {
        "GET /api/v1/accounts": page([
          account("a-8", "Car loan", { type: "loan", is_asset: false, can_edit: false }),
          account("a-9", "Shared cabin", { type: "manual_asset", can_edit: false }),
          account("a-10", "Own car loan", { type: "loan", is_asset: false }),
        ]),
      }),
    );
    renderApp("/accounts");
    const loans = await screen.findByRole("region", { name: "Loans" });
    expect(
      await within(loans).findByRole("button", { name: "Add loan terms for Own car loan" }),
    ).toBeInTheDocument();
    expect(
      within(loans).queryByRole("button", { name: "Add loan terms for Car loan" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Add asset details for Shared cabin" }),
    ).not.toBeInTheDocument();
  });

  it("a viewer sees no Add account", async () => {
    mockApi(handlers("viewer"));
    renderApp("/accounts");
    await screen.findByRole("region", { name: "Cash" });
    expect(
      screen.queryByRole("button", { name: "Add account" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Add loan terms/ }),
    ).not.toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    mockApi(handlers("owner"));
    const { container } = renderApp("/accounts");
    await screen.findByRole("region", { name: "Cash" });
    await screen.findByText("Down $250.00 this month");
    await expectNoAxeViolations(container);
  });
});
