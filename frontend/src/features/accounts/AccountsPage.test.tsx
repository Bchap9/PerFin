// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { account, page } from "../transactions/fixtures";
import { groupAccounts } from "./api";
import { groupTotal } from "./AccountsPage";

const ACCOUNTS = [
  account("a-1", "Everyday", { balance: { amount: "1200.50", currency: "USD" } }),
  account("a-2", "Rainy day", { type: "savings", balance: { amount: "5000.00", currency: "USD" } }),
  account("a-3", "Visa", { type: "credit_card", balance: { amount: "-300.25", currency: "USD" } }),
  account("a-4", "Bills", { balance: { amount: "99.50", currency: "USD" } }),
  account("a-5", "Old", { is_closed: true, closed_on: "2026-01-01" }),
];

describe("account grouping", () => {
  it("groups by type in a fixed order and sums a single currency exactly", () => {
    const groups = groupAccounts(ACCOUNTS);
    expect(groups.map((group) => group.type)).toEqual(["checking", "savings", "credit_card"]);
    expect(groups[0]?.accounts.map((item) => item.name)).toEqual(["Bills", "Everyday", "Old"]);
    expect(groupTotal(ACCOUNTS.slice(0, 1).concat(ACCOUNTS[3] ?? []))).toEqual({ amount: "1300.00", currency: "USD" });
    expect(groupTotal([account("x", "x"), account("y", "y", { balance: { amount: "1.00", currency: "EUR" } })])).toBeNull();
  });
});

describe("accounts page", () => {
  it("shows balances by group and hides closed accounts until asked", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession({ role: "owner" }), "GET /api/v1/accounts": page(ACCOUNTS) });
    renderApp("/accounts");
    const user = userEvent.setup();
    const checking = await screen.findByRole("region", { name: "Checking" });
    expect(within(checking).getByText("$1,200.50")).toBeInTheDocument();
    expect(within(checking).queryByText("Old")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Credit cards" })).toHaveTextContent("-$300.25");
    await user.click(screen.getByRole("button", { name: "Show closed accounts" }));
    expect(within(checking).getByText("Old")).toBeInTheDocument();
  });

  it("creates an account with a decimal-string opening balance", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      "GET /api/v1/accounts": page([]),
      "POST /api/v1/accounts": () => json(account("a-9", "Fresh"), 201),
    });
    renderApp("/accounts");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New account" }));
    const dialog = await screen.findByRole("dialog", { name: "New account" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Fresh");
    await user.type(within(dialog).getByLabelText("Opening balance"), "250.75");
    await user.click(within(dialog).getByRole("button", { name: "Create account" }));
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

  it("has no create button for a viewer", async () => {
    mockApi({ "GET /api/v1/auth/session": authedSession({ role: "viewer" }), "GET /api/v1/accounts": page(ACCOUNTS) });
    renderApp("/accounts");
    await screen.findByRole("region", { name: "Checking" });
    expect(screen.queryByRole("button", { name: "New account" })).not.toBeInTheDocument();
  });
});

describe("account detail", () => {
  const detail = (role: "owner" | "viewer") =>
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role }),
      "GET /api/v1/accounts/:id": () => json(ACCOUNTS[0]),
      "GET /api/v1/accounts/:id/balances": () =>
        json({
          account_id: "a-1",
          currency: "USD",
          start: "2025-11-01",
          end: "2026-11-03",
          items: [{ date: "2026-10-01", balance: { amount: "1000.00", currency: "USD" } }],
          snapshots: [
            { id: "s-1", as_of_date: "2026-10-01", balance: { amount: "1000.00", currency: "USD" }, source: "manual", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" },
          ],
        }),
      "POST /api/v1/accounts/:id/balances": () => json({}, 201),
    });

  it("records a manual balance as a decimal string", async () => {
    const api = detail("owner");
    renderApp("/accounts/a-1");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Record a balance" });
    await user.type(within(form).getByLabelText(/^Balance/), "1250.10");
    await user.click(within(form).getByRole("button", { name: "Record balance" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/accounts/:id/balances")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/accounts/:id/balances")[0]?.body).toMatchObject({ balance: { amount: "1250.10", currency: "USD" } });
    expect(screen.getByRole("img", { name: "Balance history, 1 points" })).toBeInTheDocument();
  });

  it("hides edit controls from a viewer", async () => {
    detail("viewer");
    renderApp("/accounts/a-1");
    await screen.findByRole("heading", { name: "Everyday" });
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Record a balance" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View transactions" })).toBeInTheDocument();
  });
});
