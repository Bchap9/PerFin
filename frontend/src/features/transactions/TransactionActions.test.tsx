// SPDX-License-Identifier: AGPL-3.0-or-later
/** Add, edit, delete, confirm and "Find transfers" on the transactions screen, and the row menu. */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

import { todayIsoDate } from "../../lib/dates";
import { authedSession, json, mockApi, noContent, problem, renderApp } from "../auth/testing";
import { autoPairSummary } from "./autoPair";
import { account, lookupHandlers, page, tx } from "./fixtures";

const ROWS = [tx("x-1"), tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null })];

function setup(rows = ROWS, accounts = [account("a-1", "Everyday")]) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner" }),
    ...lookupHandlers(accounts),
    "GET /api/v1/transactions": page(rows),
  });
}

/** Open a row's Actions menu and pick an item. */
async function chooseAction(user: UserEvent, row: string, action: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: `Actions for ${row}` }));
  await user.click(within(screen.getByRole("menu", { name: `Actions for ${row}` })).getByRole("menuitem", { name: action }));
}

async function openAddDialog(user: UserEvent): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: "Add transaction" }));
  return screen.findByRole("dialog", { name: "Add transaction" });
}

describe("add a transaction", () => {
  it("posts money out as a negative decimal string with a CSRF token and an idempotency key", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions", () => json(tx("x-9"), 201));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    // The only account is chosen already; the date starts on today.
    expect(within(dialog).getByLabelText(/^Account/)).toHaveValue("a-1");
    expect(within(dialog).getByLabelText(/^Date/)).toHaveValue(todayIsoDate());
    expect(within(dialog).getByRole("radio", { name: /Money out/ })).toBeChecked();
    await user.type(within(dialog).getByLabelText(/^Amount \(USD\)/), "12.5");
    await user.type(within(dialog).getByLabelText("Description"), "Farm stand");
    await user.selectOptions(within(dialog).getByLabelText("Category"), "c-2");
    await user.type(within(dialog).getByLabelText("Memo"), "cash");
    const listCalls = api.callsTo("GET", "/api/v1/transactions").length;
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(1);
    });
    const call = api.callsTo("POST", "/api/v1/transactions")[0];
    expect(call?.body).toEqual({
      account_id: "a-1",
      posted_date: todayIsoDate(),
      amount: { amount: "-12.5", currency: "USD" },
      status: "cleared",
      raw_payee: "Farm stand",
      category_id: "c-2",
      memo: "cash",
    });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(call?.headers.get("Idempotency-Key")).toMatch(/^[\x21-\x7e]{16,255}$/);
    expect(await screen.findByText("Transaction added")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Add transaction" })).not.toBeInTheDocument();
    });
    // The list is fetched again so the new row shows.
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").length).toBeGreaterThan(listCalls);
    });
  });

  it("starts on the filtered account, offers only accounts the person may edit, and posts money in as positive", async () => {
    const api = setup(ROWS, [account("a-1", "Everyday"), account("a-2", "Wallet", { type: "cash" }), account("a-3", "Partner", { can_edit: false })]);
    api.on("POST /api/v1/transactions", () => json(tx("x-9", { account_id: "a-2" }), 201));
    renderApp("/transactions?account=a-2");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    const accountSelect = within(dialog).getByLabelText(/^Account/);
    expect(accountSelect).toHaveValue("a-2");
    expect(within(accountSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["Choose an account", "Everyday", "Wallet"]);
    await user.click(within(dialog).getByRole("radio", { name: /Money in/ }));
    await user.selectOptions(within(dialog).getByLabelText("Payee"), "p-1");
    await user.type(within(dialog).getByLabelText(/^Amount/), "1,250.00");
    await user.selectOptions(within(dialog).getByLabelText("Status"), "pending");
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/transactions")[0]?.body).toEqual({
      account_id: "a-2",
      posted_date: todayIsoDate(),
      amount: { amount: "1250.00", currency: "USD" },
      status: "pending",
      payee_id: "p-1",
    });
  });

  it("asks for an account and a usable amount before sending anything", async () => {
    const api = setup(ROWS, [account("a-1", "Everyday"), account("a-2", "Wallet")]);
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    expect(within(dialog).getByText("Choose an account.")).toBeInTheDocument();
    expect(within(dialog).getByText("Enter an amount such as 12.50.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/^Amount/)).toBeInvalid();
    // Correcting a field clears its message straight away.
    await user.selectOptions(within(dialog).getByLabelText(/^Account/), "a-2");
    expect(within(dialog).queryByText("Choose an account.")).not.toBeInTheDocument();
    await user.type(within(dialog).getByLabelText(/^Amount/), "-5");
    expect(within(dialog).queryByText("Enter an amount such as 12.50.")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    expect(within(dialog).getByText("Enter the amount without a sign, then choose money out or money in.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(0);
  });

  it("shows the server's refusal, retries the same request with the same key and a changed one with a new key", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions", () => problem(422, "currency-mismatch"));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    await user.type(within(dialog).getByLabelText(/^Amount/), "5");
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("detail of currency-mismatch");
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(2);
    });
    await within(dialog).findByRole("alert");
    api.on("POST /api/v1/transactions", () => json(tx("x-9"), 201));
    await user.type(within(dialog).getByLabelText("Memo"), "second try");
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions")).toHaveLength(3);
    });
    const keys = api.callsTo("POST", "/api/v1/transactions").map((call) => call.headers.get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).toBeTruthy();
    expect(keys[2]).not.toBe(keys[0]);
    expect(await screen.findByText("Transaction added")).toBeInTheDocument();
  });

  it("puts a validation problem from the server on its field", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions", () =>
      problem(422, "validation-error", { errors: [{ loc: ["body", "raw_payee"], msg: "must be a single line", type: "value_error" }] }),
    );
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    await user.type(within(dialog).getByLabelText(/^Amount/), "5");
    await user.click(within(dialog).getByRole("button", { name: "Add transaction" }));
    expect(await within(dialog).findByText("must be a single line")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Description")).toBeInvalid();
  });

  it("opens from an account's page with that account chosen", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "owner" }),
      ...lookupHandlers([account("a-1", "Everyday"), account("a-2", "Wallet")]),
      "GET /api/v1/accounts/:id": () => json(account("a-2", "Wallet")),
      "GET /api/v1/accounts/:id/balances": () => json({ account_id: "a-2", currency: "USD", start: "2025-11-01", end: "2026-11-03", items: [], snapshots: [] }),
    });
    renderApp("/accounts/a-2");
    const user = userEvent.setup();
    const dialog = await openAddDialog(user);
    await waitFor(() => {
      expect(within(dialog).getByLabelText(/^Account/)).toHaveValue("a-2");
    });
  });
});

describe("edit a transaction", () => {
  it("starts from the row and sends only what changed", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => json(tx("x-1")));
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "Corner Market", "Edit");
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction" });
    expect(within(dialog).getByLabelText("Account")).toBeDisabled();
    expect(within(dialog).getByLabelText(/^Date/)).toHaveValue("2026-11-01");
    expect(within(dialog).getByRole("radio", { name: /Money out/ })).toBeChecked();
    const amount = within(dialog).getByLabelText(/^Amount \(USD\)/);
    expect(amount).toHaveValue("42.10");
    const description = within(dialog).getByLabelText("Description");
    expect(description).toHaveValue("CORNER MARKET 12");
    await user.clear(within(dialog).getByLabelText(/^Date/));
    await user.type(within(dialog).getByLabelText(/^Date/), "2026-11-02");
    await user.clear(amount);
    await user.type(amount, "45");
    await user.clear(description);
    await user.type(description, "Farm stand");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(1);
    });
    const call = api.callsTo("PATCH", "/api/v1/transactions/:id")[0];
    expect(call?.path).toBe("/api/v1/transactions/x-1");
    expect(call?.body).toEqual({ posted_date: "2026-11-02", amount: { amount: "-45", currency: "USD" }, raw_payee: "Farm stand" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Transaction saved")).toBeInTheDocument();
  });

  it("closes without a request when nothing was changed", async () => {
    const api = setup();
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "Corner Market", "Edit");
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction" });
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Edit transaction" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(0);
  });

  it("keeps the dialog open and says why when the server refuses", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(409, "transaction-split"));
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "Corner Market", "Edit");
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction" });
    await user.click(within(dialog).getByRole("radio", { name: /Money in/ }));
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("detail of transaction-split");
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ amount: { amount: "42.10", currency: "USD" } });
    expect(screen.getByRole("dialog", { name: "Edit transaction" })).toBeInTheDocument();
  });

  it("locks the amount of a transfer leg and still saves the other fields", async () => {
    const api = setup([tx("x-1", { transfer: { group_id: "g-1", counterpart_id: "x-7" } })]);
    api.on("PATCH /api/v1/transactions/:id", () => json(tx("x-1")));
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "Corner Market", "Edit");
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction" });
    const amount = within(dialog).getByLabelText(/^Amount/);
    expect(amount).toBeDisabled();
    expect(amount).toHaveAccessibleDescription("This is one leg of a transfer. Unlink the transfer to change the amount.");
    expect(within(dialog).getByRole("radio", { name: /Money in/ })).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Memo"), "to savings");
    await user.click(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ memo: "to savings" });
    });
  });
});

describe("delete a transaction", () => {
  it("asks first, naming the row, then deletes it", async () => {
    const api = setup();
    api.on("DELETE /api/v1/transactions/:id", () => noContent());
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "ACME", "Delete");
    const dialog = await screen.findByRole("dialog", { name: "Delete this transaction?" });
    expect(within(dialog).getByText("ACME")).toBeInTheDocument();
    expect(within(dialog).getByText(/Everyday/)).toBeInTheDocument();
    expect(within(dialog).getByText("-$42.10")).toBeInTheDocument();
    expect(api.callsTo("DELETE", "/api/v1/transactions/:id")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete transaction" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/transactions/:id")).toHaveLength(1);
    });
    const call = api.callsTo("DELETE", "/api/v1/transactions/:id")[0];
    expect(call?.path).toBe("/api/v1/transactions/x-2");
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Transaction deleted")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Delete this transaction?" })).not.toBeInTheDocument();
    });
  });

  it("deletes nothing on Cancel and shows the server's refusal", async () => {
    const api = setup();
    api.on("DELETE /api/v1/transactions/:id", () => problem(409, "transfer-conflict"));
    renderApp("/transactions");
    const user = userEvent.setup();
    await chooseAction(user, "Corner Market", "Delete");
    await user.click(within(await screen.findByRole("dialog", { name: "Delete this transaction?" })).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Delete this transaction?" })).not.toBeInTheDocument();
    });
    expect(api.callsTo("DELETE", "/api/v1/transactions/:id")).toHaveLength(0);
    await chooseAction(user, "Corner Market", "Delete");
    const dialog = await screen.findByRole("dialog", { name: "Delete this transaction?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete transaction" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("detail of transfer-conflict");
  });

  it("deletes the selected rows in bulk after a confirmation", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "delete", changed: 2, results: [] }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Select all loaded transactions"));
    await user.click(within(screen.getByRole("group", { name: "Actions for 2 selected" })).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete 2 transactions?" });
    expect(api.callsTo("POST", "/api/v1/transactions/bulk")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Delete transactions" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "delete", ids: ["x-1", "x-2"] });
    });
    expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
  });
});

describe("confirm a flagged transaction", () => {
  const flagged = [tx("x-1", { review_state: "pending" }), tx("x-2", { payee_id: null, raw_payee: "ACME" })];

  it("offers Mark confirmed on a row that needs review and patches its review state", async () => {
    const api = setup(flagged);
    let release: () => void = () => undefined;
    api.on("PATCH /api/v1/transactions/:id", async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return json(tx("x-1", { review_state: "confirmed" }));
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    const grid = await screen.findByRole("table", { name: "Transactions" });
    expect(await within(grid).findByText("Needs review")).toBeInTheDocument();
    // A row that is not flagged has nothing to confirm.
    await user.click(screen.getByRole("button", { name: "Actions for ACME" }));
    expect(within(screen.getByRole("menu")).queryByRole("menuitem", { name: "Mark confirmed" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");
    // The refetch after the save never answers: only the optimistic update can clear the badge.
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await chooseAction(user, "Corner Market", "Mark confirmed");
    await waitFor(() => {
      expect(within(grid).queryByText("Needs review")).not.toBeInTheDocument();
    });
    release();
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(1);
    });
    const call = api.callsTo("PATCH", "/api/v1/transactions/:id")[0];
    expect(call?.path).toBe("/api/v1/transactions/x-1");
    expect(call?.body).toEqual({ review_state: "confirmed" });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(await screen.findByText("Marked as confirmed")).toBeInTheDocument();
  });

  it("confirms the selected rows in bulk", async () => {
    const api = setup(flagged);
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "confirm", changed: 1, results: [] }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Select Corner Market"));
    await user.click(screen.getByRole("button", { name: "Mark confirmed" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "confirm", ids: ["x-1"] });
    });
    expect(await screen.findByText("1 transaction changed")).toBeInTheDocument();
  });
});

describe("find transfers", () => {
  it("runs the matcher, reports linked and ambiguous pairs and refreshes the list", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/transfers/auto-pair", () =>
      json({
        paired: [
          { group_id: "g-1", transaction_ids: ["x-1", "x-5"] },
          { group_id: "g-2", transaction_ids: ["x-2", "x-6"] },
        ],
        ambiguous: 1,
      }),
    );
    renderApp("/transactions");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Find transfers" });
    await screen.findByLabelText("Category of Corner Market");
    const listCalls = api.callsTo("GET", "/api/v1/transactions").length;
    await user.click(button);
    expect(await screen.findByText("2 transfers linked")).toBeInTheDocument();
    expect(screen.getByText("1 possible transfer has more than one match. Link it by hand from the row's Actions menu.")).toBeInTheDocument();
    const call = api.callsTo("POST", "/api/v1/transactions/transfers/auto-pair")[0];
    expect(call?.body).toEqual({ window_days: 3 });
    expect(call?.headers.get("X-CSRF-Token")).toBe("session-token");
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").length).toBeGreaterThan(listCalls);
    });
  });

  it("says so when there is nothing to link, and why when it fails", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/transfers/auto-pair", () => json({ paired: [], ambiguous: 0 }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Find transfers" }));
    expect(await screen.findByText("No new transfers found")).toBeInTheDocument();
    api.on("POST /api/v1/transactions/transfers/auto-pair", () => problem(403, "forbidden"));
    await user.click(screen.getByRole("button", { name: "Find transfers" }));
    expect(await screen.findByText("Could not look for transfers")).toBeInTheDocument();
    expect(screen.getByText("detail of forbidden")).toBeInTheDocument();
  });

  it("words every outcome", () => {
    const pair = { group_id: "g", transaction_ids: ["a", "b"] };
    expect(autoPairSummary({ paired: [pair], ambiguous: 0 })).toEqual({ title: "1 transfer linked", tone: "success" });
    expect(autoPairSummary({ paired: [], ambiguous: 0 })).toEqual({ title: "No new transfers found", tone: "info" });
    expect(autoPairSummary({ paired: [], ambiguous: 2 })).toEqual({
      title: "No new transfers found",
      description: "2 possible transfers have more than one match. Link them by hand from the row's Actions menu.",
      tone: "warning",
    });
  });
});

describe("row actions menu", () => {
  it("works from the keyboard and returns focus to its button", async () => {
    setup();
    renderApp("/transactions");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Actions for Corner Market" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveAttribute("aria-expanded", "false");
    button.focus();
    await user.keyboard("{ArrowDown}");
    const menu = screen.getByRole("menu", { name: "Actions for Corner Market" });
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(button).toHaveAttribute("aria-controls", menu.id);
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Edit", "Split", "Link transfer", "Always categorise like this…", "Delete"]);
    expect(within(menu).getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(within(menu).getByRole("menuitem", { name: "Split" })).toHaveFocus();
    await user.keyboard("{End}");
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(within(menu).getByRole("menuitem", { name: "Edit" })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("l");
    expect(within(menu).getByRole("menuitem", { name: "Link transfer" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
    // ArrowUp opens on the last item; Enter runs it.
    await user.keyboard("{ArrowUp}");
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Delete" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { name: "Delete this transaction?" })).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes on a click elsewhere and names the transfer action by the row's state", async () => {
    setup([tx("x-1", { transfer: { group_id: "g-1", counterpart_id: null } })]);
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Corner Market" }));
    expect(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Unlink transfer" })).toBeInTheDocument();
    await user.click(screen.getByRole("heading", { name: "Transactions" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
