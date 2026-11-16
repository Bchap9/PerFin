// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, problem, renderApp, type Handler } from "../auth/testing";
import { account, lookupHandlers, NOW, page, tx } from "../transactions/fixtures";

const EVERYDAY = account("a-1", "Everyday", {
  balance: { amount: "1200.50", currency: "USD" },
  balance_date: "2026-11-03",
});

const BALANCES = {
  account_id: "a-1",
  currency: "USD",
  start: "2025-11-01",
  end: "2026-11-03",
  items: [{ date: "2026-10-01", balance: { amount: "1000.00", currency: "USD" } }],
  snapshots: [
    {
      id: "s-1",
      as_of_date: "2026-10-01",
      balance: { amount: "1000.00", currency: "USD" },
      source: "manual",
      created_at: NOW,
      updated_at: NOW,
    },
  ],
};

const document = (id: string, extra: Record<string, unknown> = {}) => ({
  id,
  account_id: "a-1",
  created_at: NOW,
  updated_at: NOW,
  error: null,
  institution_id: null,
  kind: "statement",
  mime_detected: "application/pdf",
  original_filename: "example-statement-october.pdf",
  parser_version: null,
  period_start: "2026-10-01",
  period_end: "2026-10-31",
  sha256: "0".repeat(64),
  size: 2048,
  source: "upload",
  status: "processed",
  tax_year: null,
  unchecked: false,
  uploaded_by: null,
  ...extra,
});

function hub(role: "owner" | "member" | "viewer" = "owner", extra: Record<string, Handler> = {}) {
  return mockApi({
    ...lookupHandlers([EVERYDAY]),
    "GET /api/v1/auth/session": authedSession({ role }),
    "GET /api/v1/accounts/subtypes": () => json([{ type: "checking", subtypes: [] }]),
    "GET /api/v1/accounts/:id": () => json(EVERYDAY),
    "GET /api/v1/accounts/:id/balances": () => json(BALANCES),
    "POST /api/v1/accounts/:id/balances": () => json({}, 201),
    "GET /api/v1/transactions": page([
      tx("t-1", { merchant: "Corner Market", raw_payee: "CORNER MARKET 12" }),
      tx("t-2", { raw_payee: "EXAMPLE TRANSFER 7", payee_id: null, status: "pending", amount: { amount: "250.00", currency: "USD" } }),
    ]),
    "GET /api/v1/documents": page([
      document("d-1"),
      document("d-2", { original_filename: null, kind: "receipt", unchecked: true, period_start: null, period_end: null }),
    ]),
    ...extra,
  });
}

describe("account hub", () => {
  it("a checking account has overview, transactions and documents", async () => {
    hub();
    renderApp("/accounts/a-1");
    expect(await screen.findByRole("heading", { level: 1, name: "Everyday" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumbs" });
    expect(within(crumbs).getByRole("link", { name: "Accounts" })).toHaveAttribute("href", "/accounts");
    expect(within(crumbs).getByText("Everyday")).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Balance", { selector: "dt" })).toBeInTheDocument();
    expect(screen.getByText("$1,200.50", { selector: "dd *" })).toBeInTheDocument();
    expect(screen.getByText("Checking · Balance from Nov 3")).toBeInTheDocument();
    const tabs = within(screen.getByRole("tablist", { name: "Everyday" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Overview", "Transactions", "Documents"]);
    expect(screen.getByRole("tab", { name: "Overview", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Balance history, 1 points" })).toBeInTheDocument();
  });

  it("keeps the tab in the address", async () => {
    hub();
    const { router } = renderApp("/accounts/a-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Documents" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/accounts/a-1?tab=documents");
    });
    await user.click(screen.getByRole("tab", { name: "Overview" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/accounts/a-1");
    });
  });

  it("transactions link to the drawer", async () => {
    const api = hub();
    renderApp("/accounts/a-1?tab=transactions");
    const table = await screen.findByRole("table", { name: "Latest transactions of Everyday" });
    expect(within(table).getByRole("link", { name: "Corner Market" })).toHaveAttribute("href", "/transactions?account=a-1&tx=t-1");
    expect(within(table).getByRole("link", { name: "EXAMPLE TRANSFER 7" })).toHaveAttribute("href", "/transactions?account=a-1&tx=t-2");
    expect(within(table).getByText("Pending")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All transactions of this account" })).toHaveAttribute("href", "/transactions?account=a-1");
    const calls = api.callsTo("GET", "/api/v1/transactions");
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[0]?.search).toContain("account=a-1");
    expect(calls[0]?.search).toContain("limit=50");
  });

  it("documents link to the Inbox", async () => {
    const api = hub();
    renderApp("/accounts/a-1?tab=documents");
    const list = await screen.findByRole("list", { name: "Documents of Everyday" });
    expect(within(list).getByRole("link", { name: "example-statement-october.pdf" })).toHaveAttribute("href", "/inbox/documents/d-1");
    expect(within(list).getByRole("link", { name: "Untitled receipt" })).toHaveAttribute("href", "/inbox/documents/d-2");
    expect(within(list).getByText("Not checked against a balance")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/documents")[0]?.search).toContain("account=a-1");
  });

  it("says so when the account is gone", async () => {
    hub("owner", { "GET /api/v1/accounts/:id": () => problem(404, "not-found") });
    renderApp("/accounts/a-1");
    expect(await screen.findByRole("heading", { level: 1, name: "Account not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to accounts" })).toHaveAttribute("href", "/accounts");
  });

  it("records a manual balance as a decimal string", async () => {
    const api = hub();
    renderApp("/accounts/a-1");
    const user = userEvent.setup();
    const form = await screen.findByRole("form", { name: "Record a balance" });
    await user.type(within(form).getByLabelText(/^Balance/), "1250.10");
    await user.click(within(form).getByRole("button", { name: "Record balance" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/accounts/:id/balances")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/accounts/:id/balances")[0]?.body).toMatchObject({
      balance: { amount: "1250.10", currency: "USD" },
    });
  });

  it("puts edit, close and add transaction in the header for a writer", async () => {
    hub("member");
    renderApp("/accounts/a-1");
    const user = userEvent.setup();
    expect(await screen.findByRole("button", { name: "Add transaction" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Actions for Everyday" }));
    const menu = screen.getByRole("menu", { name: "Actions for Everyday" });
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Edit", "Close account"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Edit" }));
    expect(await screen.findByRole("dialog", { name: /Edit/ })).toBeInTheDocument();
  });

  it("hides edit controls from a viewer", async () => {
    hub("viewer");
    renderApp("/accounts/a-1");
    await screen.findByRole("heading", { level: 1, name: "Everyday" });
    expect(screen.queryByRole("button", { name: "Actions for Everyday" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add transaction" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Record a balance" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Transactions" })).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    hub();
    const { container } = renderApp("/accounts/a-1");
    await screen.findByRole("img", { name: "Balance history, 1 points" });
    await screen.findByRole("table", { name: "Recorded balances of Everyday" });
    await expectNoAxeViolations(container);
  });
});
