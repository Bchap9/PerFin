// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { account, category, lookupHandlers, page, summary, tx } from "./fixtures";
import { authedSession, json, mockApi, problem, renderApp, type SessionOptions } from "../auth/testing";
import { patchPages } from "./optimistic";

function setup(role: SessionOptions["role"] = "owner", rows = [tx("x-1"), tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null })]) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": page(rows),
  });
}

describe("patchPages", () => {
  it("patches only the matching row", () => {
    const data = { pageParams: [null], pages: [{ items: [tx("a"), tx("b")], next_cursor: null }] };
    const next = patchPages(data, "b", { category_id: "c-2", tag_ids: ["t-1"] });
    expect(next?.pages[0]?.items.map((row) => row.category_id)).toEqual(["c-1", "c-2"]);
    expect(next?.pages[0]?.items[1]?.tag_ids).toEqual(["t-1"]);
    expect(data.pages[0]?.items[1]?.category_id).toBe("c-1");
  });

  it("a category chosen by hand is the person's and clears the suggestion", () => {
    const row = tx("a", { category_id: null, suggested_category_id: "c-2", category_source: "dictionary", category_confidence: "0.60", category_rule_id: "r-1" });
    const data = { pageParams: [null], pages: [{ items: [row], next_cursor: null }] };
    for (const chosen of ["c-1", null]) {
      const next = patchPages(data, "a", { category_id: chosen })?.pages[0]?.items[0];
      expect(next).toMatchObject({ category_id: chosen, category_source: "person", category_confidence: "1.00", category_rule_id: null, suggested_category_id: null });
    }
    // Another field leaves the provenance alone.
    expect(patchPages(data, "a", { notes: "n" })?.pages[0]?.items[0]).toMatchObject({ category_source: "dictionary", suggested_category_id: "c-2" });
  });
});

describe("transactions grid", () => {
  it("lists rows and passes filters to the API with a cursor for more", async () => {
    const api = setup("owner");
    let calls = 0;
    api.on("GET /api/v1/transactions", () => {
      calls += 1;
      return json({ items: [tx(`r-${String(calls)}`)], next_cursor: calls === 1 ? "cur-1" : null });
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    expect(await screen.findByLabelText("Category of Corner Market")).toHaveValue("c-1");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").some((call) => call.search.includes("cursor=cur-1"))).toBe(true);
    });
    await user.selectOptions(screen.getByLabelText("Account"), "a-1");
    await user.type(screen.getByLabelText("Search"), "market");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).toContain("account=a-1");
      expect(last?.search).toContain("q=market");
    });
  });

  it("applies an inline edit at once and keeps it when the server accepts it", async () => {
    const api = setup();
    api.on("PATCH", () => json({}));
    let release: () => void = () => undefined;
    api.on("PATCH /api/v1/transactions/:id", async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return json(tx("x-1", { category_id: "c-2" }));
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Category of Corner Market");
    await user.selectOptions(select, "c-2");
    // Optimistic: the new value shows before the server has answered.
    expect(screen.getByLabelText("Category of Corner Market")).toHaveValue("c-2");
    release();
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ category_id: "c-2" });
  });

  it("rolls the cell back and says why when the server refuses the edit", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(404, "category-not-found"));
    renderApp("/transactions");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Category of Corner Market");
    // The refetch after the failure never answers, so only the rollback can restore the value.
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.selectOptions(select, "c-2");
    expect(await screen.findByText("Could not save the change")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Category of Corner Market")).toHaveValue("c-1");
    });
  });

  it("rolls back a note edit too", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(500, "internal-error"));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit notes of Corner Market" }));
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.type(screen.getByLabelText("Notes of Corner Market"), "weekly shop{Enter}");
    expect(await screen.findByText("Could not save the change")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Edit notes of Corner Market" })).toHaveTextContent("Add note");
    });
  });

  it("gives a viewer a read-only grid", async () => {
    setup("viewer");
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    await within(grid).findByText("Groceries");
    expect(screen.queryByLabelText("Category of Corner Market")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add transaction" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Find transfers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByText("You have read-only access.")).toBeInTheDocument();
  });

  it("does not offer edits on an account the member may only view", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers([account("a-1", "Everyday", { can_edit: false })]),
      "GET /api/v1/transactions": page([tx("x-1")]),
    });
    renderApp("/transactions");
    await within(await screen.findByRole("table", { name: "Transactions" })).findByText("Groceries");
    expect(screen.queryByLabelText("Category of Corner Market")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add transaction" })).not.toBeInTheDocument();
  });

  it("sends a bulk categorise for the selected rows", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "categorise", changed: 2, results: [] }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Select all loaded transactions"));
    await user.selectOptions(screen.getByLabelText("Bulk category"), "c-2");
    await user.click(screen.getByRole("button", { name: "Set category" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "categorise", ids: ["x-1", "x-2"], category_id: "c-2" });
    });
    expect(await screen.findByText("2 transactions changed")).toBeInTheDocument();
  });
});

describe("how a category was decided", () => {
  it("shows how a category was decided", async () => {
    const api = setup("owner", [
      tx("x-1", { category_source: "dictionary", category_confidence: "0.90" }),
      tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: "c-2", category_source: "rule", category_rule_id: "r-1", category_confidence: "1.00" }),
      tx("x-3", { payee_id: null, raw_payee: "BOLT HARDWARE", category_source: "rule", category_rule_id: null }),
      tx("x-4", { payee_id: null, raw_payee: "OLD ROW", category_source: "legacy" }),
    ]);
    api.on("GET /api/v1/rules", () => json({ items: [{ id: "r-1", name: "Coffee" }] }));
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    const first = within(await within(grid).findByRole("row", { name: /Corner Market/ }));
    expect(first.getByText("Merchant list")).toBeInTheDocument();
    expect(first.getByText("90%")).toBeInTheDocument();
    const second = within(within(grid).getByRole("row", { name: /ACME/ }));
    expect(await second.findByText("Rule: Coffee")).toBeInTheDocument();
    // A decision is not a guess: no percentage beside it.
    expect(second.queryByText("100%")).not.toBeInTheDocument();
    expect(within(within(grid).getByRole("row", { name: /BOLT HARDWARE/ })).getByText("A rule that no longer exists")).toBeInTheDocument();
    // A row from before this release says nothing about its source.
    expect(within(grid).getByRole("row", { name: /OLD ROW/ }).querySelector(".cat-cell__why")).toBeNull();
  });

  it("shows a suggestion as a question and accepts it", async () => {
    const suggested = tx("x-2", {
      payee_id: null,
      raw_payee: "ACME",
      category_id: null,
      suggested_category_id: "c-2",
      category_source: "dictionary",
      category_confidence: "0.60",
    });
    const api = setup("owner", [tx("x-1"), suggested]);
    api.on("POST /api/v1/transactions/bulk", () => {
      api.on(
        "GET /api/v1/transactions",
        page([tx("x-1"), { ...suggested, category_id: "c-2", suggested_category_id: null, category_source: "person", category_confidence: "1.00" }]),
      );
      return json({ action: "accept_suggestions", changed: 1, results: [{ id: "x-2", result: "updated" }] });
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    const grid = await screen.findByRole("table", { name: "Transactions" });
    const row = within(await within(grid).findByRole("row", { name: /ACME/ }));
    expect(row.getByText("Dining?")).toBeInTheDocument();
    expect(row.getByText("Suggestion")).toBeInTheDocument();
    expect(row.getByText("60%")).toBeInTheDocument();
    // It never looks like a set category: the picker holds no value.
    expect(row.getByLabelText("Category of ACME")).toHaveValue("");
    await user.click(row.getByRole("button", { name: "Accept the suggestion for ACME" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "accept_suggestions", ids: ["x-2"] });
    });
    const after = within(within(grid).getByRole("row", { name: /ACME/ }));
    expect(await after.findByText("You chose this")).toBeInTheDocument();
    await waitFor(() => {
      expect(after.getByLabelText("Category of ACME")).toHaveValue("c-2");
    });
    expect(after.queryByText("Dining?")).not.toBeInTheDocument();
    expect(after.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
  });

  it("choosing another category for a suggestion is the person's choice", async () => {
    const api = setup("owner", [tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null, suggested_category_id: "c-2" })]);
    api.on("PATCH /api/v1/transactions/:id", () => new Promise<Response>(() => undefined));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Category of ACME"), "c-1");
    // Optimistic: the suggestion is gone and the row says who decided.
    expect(await screen.findByText("You chose this")).toBeInTheDocument();
    expect(screen.queryByText("Dining?")).not.toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ category_id: "c-1" });
  });

  it("puts the suggestion back and says why when the server refuses it", async () => {
    const api = setup("owner", [tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null, suggested_category_id: "c-2" })]);
    api.on("POST /api/v1/transactions/bulk", () => problem(403, "forbidden"));
    renderApp("/transactions");
    const user = userEvent.setup();
    const accept = await screen.findByRole("button", { name: "Accept the suggestion for ACME" });
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.click(accept);
    expect(await screen.findByText("Could not accept the suggestion")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("Dining?")).toBeInTheDocument();
    });
  });

  it("says so when the suggestion was already gone", async () => {
    const api = setup("owner", [tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null, suggested_category_id: "c-2" })]);
    api.on("POST /api/v1/transactions/bulk", () =>
      json({ action: "accept_suggestions", changed: 0, results: [{ id: "x-2", result: "skipped", reason: "suggestion-missing" }] }),
    );
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Accept the suggestion for ACME" }));
    expect(await screen.findByText("That suggestion is no longer there")).toBeInTheDocument();
  });

  it("the raw descriptor stays visible beside the merchant name", async () => {
    setup("owner", [
      tx("x-1", { payee_id: null, raw_payee: "SQ *BLUE KETTLE 0042 SPRINGFIELD", merchant: "Blue Kettle", merchant_key: "blue kettle" }),
      tx("x-2", { raw_payee: "CORNER MARKET 12", merchant: "Corner Market", merchant_key: "corner market" }),
      tx("x-3", { payee_id: null, raw_payee: "Lantern Books", merchant: "Lantern Books", merchant_key: "lantern books" }),
    ]);
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    const first = within(await within(grid).findByRole("row", { name: /BLUE KETTLE/ }));
    expect(first.getByText("Blue Kettle")).toBeInTheDocument();
    expect(first.getByText("SQ *BLUE KETTLE 0042 SPRINGFIELD")).toBeInTheDocument();
    // With a payee chosen the descriptor is still on the row, and the merchant is not said twice.
    const second = within(grid).getByRole("row", { name: /CORNER MARKET 12/ });
    expect(within(second).getByText("CORNER MARKET 12")).toBeInTheDocument();
    expect(second.querySelector(".payee-cell__merchant")).toBeNull();
    // A merchant name that is the descriptor is shown once.
    expect(within(within(grid).getByRole("row", { name: /Lantern Books/ })).getAllByText("Lantern Books")).toHaveLength(1);
  });

  it("filters to suggestions", async () => {
    const api = setup();
    api.on("GET /api/v1/categorisation/summary", () => json(summary({ suggestions: 3, uncategorised: 12 })));
    renderApp("/transactions");
    const user = userEvent.setup();
    const toggle = await screen.findByRole("button", { name: "Suggestions (3)" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(api.callsTo("GET", "/api/v1/transactions")[0]?.search).not.toContain("suggested");
    await user.click(toggle);
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).toContain("suggested=true");
    });
    expect(screen.getByRole("button", { name: "Suggestions (3)" })).toHaveAttribute("aria-pressed", "true");
    // The other toggle is the existing "Uncategorised" filter.
    await user.click(screen.getByRole("button", { name: "No category (12)" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).toContain("uncategorized=true");
    });
    expect(screen.getByLabelText("Category filter")).toHaveValue("none");
    await user.click(screen.getByRole("button", { name: "Suggestions (3)" }));
    await user.click(screen.getByRole("button", { name: "No category (12)" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).not.toContain("suggested");
      expect(last?.search).not.toContain("uncategorized");
    });
  });

  it("offers the two toggles without counts when the summary is not there", async () => {
    setup();
    renderApp("/transactions");
    await screen.findByLabelText("Category of Corner Market");
    expect(screen.getByRole("button", { name: "Suggestions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "No category" })).toBeInTheDocument();
  });

  it("the category list is grouped", async () => {
    const api = setup();
    api.on(
      "GET /api/v1/categories",
      page([
        category("c-9", "Food"),
        category("c-1", "Groceries", { parent_id: "c-9" }),
        category("c-2", "Dining", { parent_id: "c-9" }),
        category("c-8", "Gifts"),
      ]),
    );
    renderApp("/transactions");
    const select = await screen.findByLabelText("Category of Corner Market");
    const group = within(select).getByRole("group", { name: "Food" });
    expect(within(group).getAllByRole("option").map((option) => option.textContent)).toEqual(["Food", "Dining", "Groceries"]);
    // A top-level category with nothing under it is an option of its own.
    expect(within(select).getAllByRole("group")).toHaveLength(1);
    expect(within(select).getByRole("option", { name: "Gifts" })).toHaveValue("c-8");
    expect(within(select).getByRole("option", { name: "Uncategorised" })).toHaveValue("");
    expect(select).toHaveValue("c-1");
  });

  it("a viewer sees the source and no accept button", async () => {
    setup("viewer", [
      tx("x-1", { category_source: "learned", category_confidence: "0.95" }),
      tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null, suggested_category_id: "c-2", category_confidence: "0.60" }),
    ]);
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    expect(await within(grid).findByText("Learned from you")).toBeInTheDocument();
    expect(within(grid).getByText("95%")).toBeInTheDocument();
    expect(within(grid).getByText("Dining?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Category of ACME")).not.toBeInTheDocument();
  });
});

describe("rows of one file", () => {
  it("reads the document filter from the address and sends it", async () => {
    const api = setup();
    renderApp("/transactions?document=d-7&account=a-1");
    const user = userEvent.setup();
    await screen.findByLabelText("Category of Corner Market");
    const first = api.callsTo("GET", "/api/v1/transactions")[0];
    expect(first?.search).toContain("document=d-7");
    expect(first?.search).toContain("account=a-1");
    // The chip says the list is narrowed and leads back to the file.
    expect(screen.getByRole("link", { name: "From one file" })).toHaveAttribute("href", "/documents/d-7");
    // Other filters are applied on top of it.
    await user.type(screen.getByLabelText("Search"), "market");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).toContain("q=market");
      expect(last?.search).toContain("document=d-7");
    });
    await user.click(screen.getByRole("button", { name: "Remove the file filter" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).not.toContain("document=");
      expect(last?.search).toContain("q=market");
    });
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
    // The filter went with its chip: applying the form does not bring it back.
    await user.type(screen.getByLabelText("Search"), "s");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).toContain("q=markets");
    });
    expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).not.toContain("document=");
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
  });

  it("Clear drops the file filter with the others", async () => {
    const api = setup();
    renderApp("/transactions?document=d-7&account=a-1");
    const user = userEvent.setup();
    await screen.findByLabelText("Category of Corner Market");
    expect(screen.getByRole("link", { name: "From one file" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).not.toContain("document=");
      expect(last?.search).not.toContain("account=");
    });
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
    // Nothing is left in the draft to bring it back.
    await user.type(screen.getByLabelText("Search"), "tram");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).toContain("q=tram");
    });
    expect(api.callsTo("GET", "/api/v1/transactions").at(-1)?.search).not.toContain("document=");
  });

  it("shows no chip and sends no document without one in the address", async () => {
    const api = setup();
    renderApp("/transactions");
    await screen.findByLabelText("Category of Corner Market");
    expect(api.callsTo("GET", "/api/v1/transactions")[0]?.search).not.toContain("document=");
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
  });

  it("the dialog links to the source document", async () => {
    setup("owner", [tx("x-1", { source_document_id: "d-7" }), tx("x-2", { payee_id: null, raw_payee: "ACME" })]);
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Corner Market" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for Corner Market" })).getByRole("menuitem", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByRole("link", { name: "Imported from a document: open it" })).toHaveAttribute("href", "/documents/d-7?row=x-1");
    await user.click(dialog.getByRole("button", { name: "Cancel" }));
    // A row typed by hand came from no file.
    await user.click(screen.getByRole("button", { name: "Actions for ACME" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for ACME" })).getByRole("menuitem", { name: "Edit" }));
    expect(within(await screen.findByRole("dialog")).queryByRole("link", { name: /Imported from a document/ })).not.toBeInTheDocument();
  });
});

describe("split editor", () => {
  it("blocks saving until the lines add up, then posts decimal strings", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/:id/split", () => json(tx("x-1")));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Corner Market" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for Corner Market" })).getByRole("menuitem", { name: "Split" }));
    const dialog = await screen.findByRole("dialog", { name: "Split transaction" });
    const first = within(within(dialog).getByRole("group", { name: "Split line 1" })).getByLabelText("Amount");
    const second = within(within(dialog).getByRole("group", { name: "Split line 2" })).getByLabelText("Amount");
    await user.clear(first);
    await user.type(first, "-40.00");
    await user.type(second, "-1.00");
    expect(within(dialog).getByRole("button", { name: "Save split" })).toBeDisabled();
    expect(within(dialog).getByText("The lines are off by -1.10.")).toBeInTheDocument();
    await user.clear(second);
    await user.type(second, "-2.10");
    expect(within(dialog).getByText("The lines add up.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save split" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/:id/split")).toHaveLength(1);
    });
    const body = api.callsTo("POST", "/api/v1/transactions/:id/split")[0]?.body as { splits: { amount: unknown }[] };
    expect(body.splits.map((split) => split.amount)).toEqual([
      { amount: "-40.00", currency: "USD" },
      { amount: "-2.10", currency: "USD" },
    ]);
  });
});
