// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { account, lookupHandlers, page, summary, tx } from "./fixtures";
import { authedSession, currentHref, json, mockApi, problem, renderApp, type SessionOptions } from "../auth/testing";
import { patchPages } from "./optimistic";

function setup(role: SessionOptions["role"] = "owner", rows = [tx("x-1"), tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null })]) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": page(rows),
    "GET /api/v1/transactions/:id": (call) => {
      const row = rows.find((candidate) => call.path.endsWith(`/${candidate.id}`));
      return row === undefined ? problem(404, "not-found") : json(row);
    },
  });
}

const lastList = (api: ReturnType<typeof setup>): string => api.callsTo("GET", "/api/v1/transactions").at(-1)?.search ?? "";

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

describe("filters in the address", () => {
  it("a filter change is written to the address", async () => {
    const api = setup();
    const { router } = renderApp("/transactions");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    // The search field is the first control under the page's header: only the
    // header's own actions (Find transfers, Add transaction) come before it.
    const main = screen.getByRole("main");
    expect(within(main).getByRole("heading", { name: "Transactions" })).toBeInTheDocument();
    const controls = Array.from(main.querySelectorAll<HTMLElement>("a[href], button, input, select, textarea, [tabindex]")).filter(
      (element) => element.tabIndex >= 0 && element.closest(".page-header") === null,
    );
    expect(controls[0]).toBe(screen.getByRole("searchbox", { name: "Search transactions" }));
    await user.selectOptions(screen.getByLabelText("Account"), "a-1");
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions?account=a-1");
    });
    await user.click(screen.getByRole("button", { name: "Suggestions" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions?account=a-1&suggested=true");
    });
    await user.type(screen.getByRole("searchbox", { name: "Search transactions" }), "market{Enter}");
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions?account=a-1&suggested=true&q=market");
    });
    await user.selectOptions(screen.getByLabelText("Category"), "c-2");
    await user.click(screen.getByText("More filters"));
    await user.type(screen.getByLabelText("From"), "2026-10-01");
    await waitFor(() => {
      expect(currentHref(router)).toContain("category=c-2");
    });
    expect(currentHref(router)).toContain("date_from=2026-10-01");
    await waitFor(() => {
      const last = lastList(api);
      expect(last).toContain("account=a-1");
      expect(last).toContain("suggested=true");
      expect(last).toContain("q=market");
      expect(last).toContain("category=c-2");
      expect(last).toContain("date_from=2026-10-01");
    });
    // Written with replace: a filter is not a new page.
    expect(router.history.length).toBe(1);
    // "Clear filters" takes them all out.
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions");
    });
    expect(screen.getByRole("searchbox", { name: "Search transactions" })).toHaveValue("");
  });

  it("keeps search, the chips, Account and Category on one bar and the dates under More filters", async () => {
    setup();
    renderApp("/transactions");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    const bar = screen.getByRole("searchbox", { name: "Search transactions" }).closest(".tx-bar");
    expect(bar).not.toBeNull();
    const inBar = within(bar as HTMLElement);
    expect(inBar.getByRole("button", { name: "Suggestions" })).toBeInTheDocument();
    expect(inBar.getByLabelText("Account")).toBeInTheDocument();
    expect(inBar.getByLabelText("Category")).toBeInTheDocument();
    const more = screen.getByLabelText("From").closest("details");
    expect(more).not.toBeNull();
    expect(more).toBe(screen.getByLabelText("To").closest("details"));
    expect(more).not.toHaveAttribute("open");
    expect(within(more as HTMLElement).getByText("More filters")).toBeInTheDocument();
    // Add transaction and Find transfers are the page header's actions.
    const header = screen.getByRole("heading", { name: "Transactions" }).closest(".page-header");
    expect(within(header as HTMLElement).getByRole("button", { name: "Add transaction" })).toBeInTheDocument();
    expect(within(header as HTMLElement).getByRole("button", { name: "Find transfers" })).toBeInTheDocument();
    await user.click(within(more as HTMLElement).getByText("More filters"));
    expect(more).toHaveAttribute("open");
  });

  it("a date in the address opens More filters, and a closed panel says what it hides", async () => {
    setup();
    renderApp("/transactions?date_from=2026-10-01&date_to=2026-10-31");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    const from = screen.getByLabelText("From");
    expect(from).toHaveValue("2026-10-01");
    expect(screen.getByLabelText("To")).toHaveValue("2026-10-31");
    const more = from.closest("details") as HTMLElement;
    expect(more).toHaveAttribute("open");
    await user.click(within(more).getByText(/More filters/));
    expect(more).not.toHaveAttribute("open");
    // Nothing is hidden silently: the summary counts the filters inside.
    expect(within(more).getByText("More filters (2)")).toBeInTheDocument();
  });

  it("reads the filters of a link, payee, tag and review state included", async () => {
    const api = setup();
    renderApp("/transactions?payee=p-1&tag=t-1&review_state=pending&merchant=quiet%20bean%20coffee");
    await screen.findByRole("row", { name: /ACME/ });
    const first = api.callsTo("GET", "/api/v1/transactions")[0]?.search ?? "";
    expect(first).toContain("payee=p-1");
    expect(first).toContain("tag=t-1");
    expect(first).toContain("review_state=pending");
    expect(first).toMatch(/merchant=quiet(\+|%20)bean(\+|%20)coffee/);
    expect(screen.getByLabelText("Payee")).toHaveValue("p-1");
    expect(screen.getByLabelText("Tag")).toHaveValue("t-1");
    expect(screen.getByLabelText("Review state")).toHaveValue("pending");
    expect(screen.getByText("Merchant: quiet bean coffee")).toBeInTheDocument();
  });

  it("the rail link clears the filters", async () => {
    const api = setup();
    api.on("GET /api/v1/categorisation/summary", () => json(summary({ suggestions: 3, uncategorised: 12 })));
    const { router } = renderApp("/transactions?account=a-1&category=none&q=market");
    const user = userEvent.setup();
    expect(await screen.findByRole("button", { name: "No category (12)" })).toHaveAttribute("aria-pressed", "true");
    expect(lastList(api)).toContain("uncategorized=true");
    await user.click(within(screen.getByRole("navigation", { name: "Sections" })).getByRole("link", { name: "Transactions" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions");
    });
    await waitFor(() => {
      const last = lastList(api);
      expect(last).not.toContain("account=");
      expect(last).not.toContain("uncategorized");
      expect(last).not.toContain("q=");
    });
    expect(screen.getByRole("button", { name: "No category (12)" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByLabelText("Account")).toHaveValue("");
    expect(screen.getByRole("searchbox", { name: "Search transactions" })).toHaveValue("");
  });

  it("the total row sums the shown rows", async () => {
    const rows = [
      tx("x-1", { amount: { amount: "-42.10", currency: "USD" } }),
      tx("x-2", { payee_id: null, raw_payee: "REFUND ACME", amount: { amount: "12.30", currency: "USD" } }),
      tx("x-3", { payee_id: null, raw_payee: "TO SAVINGS", amount: { amount: "-100.00", currency: "USD" }, transfer: { group_id: "g-1", counterpart_id: null } }),
    ];
    setup("owner", rows);
    renderApp("/transactions?account=a-1");
    expect(await screen.findByText("In $12.30 · Out $42.10 · Net -$29.80, of the 3 shown")).toBeInTheDocument();
    expect(screen.getByText("1 transfer between your accounts is left out of in and out.")).toBeInTheDocument();
  });

  it("shows no total without a filter", async () => {
    setup();
    renderApp("/transactions");
    await screen.findByRole("row", { name: /ACME/ });
    expect(screen.queryByText(/of the \d+ shown/)).not.toBeInTheDocument();
  });

  it("does not add up rows of different currencies", async () => {
    setup("owner", [tx("x-1"), tx("x-2", { payee_id: null, raw_payee: "ACME", amount: { amount: "-5.00", currency: "EUR" } })]);
    renderApp("/transactions?q=a");
    expect(await screen.findByText("The 2 shown are in more than one currency, so they are not added up.")).toBeInTheDocument();
  });

  it("filters to suggestions and to no category with their counts", async () => {
    const api = setup();
    api.on("GET /api/v1/categorisation/summary", () => json(summary({ suggestions: 3, uncategorised: 12 })));
    const { router } = renderApp("/transactions");
    const user = userEvent.setup();
    const toggle = await screen.findByRole("button", { name: "Suggestions (3)" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(api.callsTo("GET", "/api/v1/transactions")[0]?.search).not.toContain("suggested");
    await user.click(toggle);
    await waitFor(() => {
      expect(lastList(api)).toContain("suggested=true");
    });
    expect(screen.getByRole("button", { name: "Suggestions (3)" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "No category (12)" }));
    await waitFor(() => {
      expect(lastList(api)).toContain("uncategorized=true");
    });
    expect(currentHref(router)).toBe("/transactions?category=none&suggested=true");
    await user.click(screen.getByRole("button", { name: "Suggestions (3)" }));
    await user.click(screen.getByRole("button", { name: "No category (12)" }));
    await waitFor(() => {
      const last = lastList(api);
      expect(last).not.toContain("suggested");
      expect(last).not.toContain("uncategorized");
    });
  });

  it("offers the two toggles without counts when the summary is not there", async () => {
    setup();
    renderApp("/transactions");
    await screen.findByRole("row", { name: /ACME/ });
    expect(screen.getByRole("button", { name: "Suggestions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "No category" })).toBeInTheDocument();
  });
});

describe("transactions grid", () => {
  it("names each cell's part of the row, so a phone can lay a row out in two lines", async () => {
    setup("owner");
    renderApp("/transactions");
    const row = await screen.findByRole("row", { name: /ACME/ });
    // Still a table for screen readers: the column headers stay.
    expect(screen.getByRole("columnheader", { name: "Merchant" })).toBeInTheDocument();
    for (const part of ["select", "date", "merchant", "category", "account", "amount", "actions"]) {
      expect(row.querySelector(`.tx-col-${part}`), part).not.toBeNull();
    }
  });

  it("lists rows and asks for more with a cursor", async () => {
    const api = setup("owner");
    let calls = 0;
    api.on("GET /api/v1/transactions", () => {
      calls += 1;
      return json({ items: [tx(`r-${String(calls)}`)], next_cursor: calls === 1 ? "cur-1" : null });
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Load more" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").some((call) => call.search.includes("cursor=cur-1"))).toBe(true);
    });
  });

  it("arrow keys move between rows and Enter opens the row", async () => {
    setup();
    renderApp("/transactions");
    const user = userEvent.setup();
    const first = await screen.findByRole("button", { name: "Open Corner Market, Nov 1, 2026, -$42.10" });
    const second = screen.getByRole("button", { name: "Open ACME, Nov 1, 2026, -$42.10" });
    // One row is in the tab order at a time.
    expect(first).toHaveAttribute("tabindex", "0");
    expect(second).toHaveAttribute("tabindex", "-1");
    first.focus();
    await user.keyboard("{ArrowDown}");
    expect(second).toHaveFocus();
    expect(second).toHaveAttribute("tabindex", "0");
    expect(first).toHaveAttribute("tabindex", "-1");
    // The last row stays the last.
    await user.keyboard("{ArrowDown}");
    expect(second).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(first).toHaveFocus();
    await user.keyboard("{ArrowDown}{Enter}");
    // Enter opens the row's drawer.
    const drawer = await screen.findByRole("dialog", { name: "ACME" });
    expect(within(drawer).getByText("Bank wrote")).toBeInTheDocument();
  });

  it("the category cell shows its source and confidence", async () => {
    const api = setup("owner", [
      tx("x-1", { category_source: "dictionary", category_confidence: "0.90" }),
      tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: "c-2", category_source: "rule", category_rule_id: "r-1", category_confidence: "1.00" }),
      tx("x-3", { payee_id: null, raw_payee: "BOLT HARDWARE", category_source: "rule", category_rule_id: null }),
      tx("x-4", { payee_id: null, raw_payee: "OLD ROW", category_source: "legacy" }),
      tx("x-5", { payee_id: null, raw_payee: "CHECK 1042", category_id: null }),
    ]);
    api.on("GET /api/v1/rules", () => json({ items: [{ id: "r-1", name: "Coffee" }] }));
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    const first = within(await within(grid).findByRole("row", { name: /Corner Market/ }));
    // The category is a chip in its group's colour.
    expect(first.getByText("Groceries").closest(".chip")?.className).toMatch(/chip--s[1-6]/);
    expect(first.getByText("Merchant list")).toBeInTheDocument();
    expect(first.getByText("90%")).toBeInTheDocument();
    const second = within(within(grid).getByRole("row", { name: /ACME/ }));
    expect(await second.findByText("Rule: Coffee")).toBeInTheDocument();
    // A decision is not a guess: no percentage beside it.
    expect(second.queryByText("100%")).not.toBeInTheDocument();
    expect(within(within(grid).getByRole("row", { name: /BOLT HARDWARE/ })).getByText("A rule that no longer exists")).toBeInTheDocument();
    // A row from before this release says nothing about its source.
    expect(within(grid).getByRole("row", { name: /OLD ROW/ }).querySelector(".cat-cell__why")).toBeNull();
    expect(within(within(grid).getByRole("row", { name: /CHECK 1042/ })).getByText("Uncategorised")).toBeInTheDocument();
    // The inline select is gone: a category changes in the drawer or the bulk bar.
    expect(screen.queryByLabelText(/^Category of/)).not.toBeInTheDocument();
  });

  it("a suggestion is a question with Accept", async () => {
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
    // It never looks like a chosen category: no colour chip.
    expect(row.getByText("Dining?").closest(".chip")).toBeNull();
    expect(row.getByText("Suggestion")).toBeInTheDocument();
    expect(row.getByText("60%")).toBeInTheDocument();
    await user.click(row.getByRole("button", { name: "Accept the suggestion for ACME" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "accept_suggestions", ids: ["x-2"] });
    });
    const after = within(within(grid).getByRole("row", { name: /ACME/ }));
    expect(await after.findByText("You chose this")).toBeInTheDocument();
    expect(after.getByText("Dining").closest(".chip")).not.toBeNull();
    expect(after.queryByText("Dining?")).not.toBeInTheDocument();
    expect(after.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
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

  it("the raw descriptor stays visible", async () => {
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
    const second = within(within(grid).getByRole("row", { name: /CORNER MARKET 12/ }));
    expect(second.getByText("CORNER MARKET 12")).toBeInTheDocument();
    expect(second.getAllByText("Corner Market")).toHaveLength(1);
    // A merchant name that is the descriptor is shown once.
    expect(within(within(grid).getByRole("row", { name: /Lantern Books/ })).getAllByText("Lantern Books")).toHaveLength(1);
  });

  it("keeps the row's state, tags and note on the row", async () => {
    setup("owner", [
      tx("x-1", { status: "pending", review_state: "pending", tag_ids: ["t-1"], notes: "for the trip", transfer: { group_id: "g-1", counterpart_id: null } }),
    ]);
    renderApp("/transactions");
    const row = within(await screen.findByRole("row", { name: /Corner Market/ }));
    expect(row.getByText("Pending")).toBeInTheDocument();
    expect(row.getByText("Needs review")).toBeInTheDocument();
    expect(row.getByText("Transfer")).toBeInTheDocument();
    expect(row.getByText("trip")).toBeInTheDocument();
    expect(row.getByText("for the trip")).toBeInTheDocument();
  });

  it("edits a note from the row menu and rolls it back when the server refuses", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(500, "internal-error"));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Corner Market" }));
    await user.click(screen.getByRole("menuitem", { name: "Add note…" }));
    const dialog = await screen.findByRole("dialog", { name: "Note on Corner Market" });
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.type(within(dialog).getByLabelText("Note"), "weekly shop");
    await user.click(within(dialog).getByRole("button", { name: "Save note" }));
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ notes: "weekly shop" });
    expect(await screen.findByText("Could not save the change")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("weekly shop")).not.toBeInTheDocument();
    });
  });

  it("gives a viewer a read-only grid", async () => {
    setup("viewer");
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    await within(grid).findByText("Groceries");
    expect(screen.queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
    // A row still opens, to a drawer that only shows.
    expect(screen.getByRole("button", { name: "Open Corner Market, Nov 1, 2026, -$42.10" })).toBeInTheDocument();
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
  });

  it("the row menu's Change category… opens the drawer", async () => {
    setup();
    const { router } = renderApp("/transactions?account=a-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for ACME" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for ACME" })).getByRole("menuitem", { name: "Change category…" }));
    expect(await screen.findByRole("dialog", { name: "ACME" })).toBeInTheDocument();
    expect(currentHref(router)).toBe("/transactions?account=a-1&tx=x-2");
  });

  it("a category whose name did not load is not called Uncategorised", async () => {
    const api = setup();
    api.on("GET /api/v1/categories", () => problem(500, "internal"));
    renderApp("/transactions");
    const row = await screen.findByRole("row", { name: /Corner Market/ });
    expect(within(row).queryByText("Uncategorised")).not.toBeInTheDocument();
    expect(within(row).getByText("Category not loaded")).toBeInTheDocument();
    // A row with no category still says so.
    expect(within(screen.getByRole("row", { name: /ACME/ })).getByText("Uncategorised")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    setup("owner", [
      tx("x-1", { category_source: "dictionary", category_confidence: "0.90", status: "pending" }),
      tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null, suggested_category_id: "c-2", category_source: "dictionary", category_confidence: "0.60" }),
    ]);
    const { container } = renderApp("/transactions?account=a-1");
    await screen.findByText("Dining?");
    await screen.findByText(/of the 2 shown/);
    // One h1 on the page.
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    await expectNoAxeViolations(container);
  });
});

describe("rows of one file or one subscription", () => {
  it("reads the document filter from the address, sends it and drops it with its chip", async () => {
    const api = setup();
    const { router } = renderApp("/transactions?document=d-7&account=a-1");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    const first = api.callsTo("GET", "/api/v1/transactions")[0];
    expect(first?.search).toContain("document=d-7");
    expect(first?.search).toContain("account=a-1");
    // The chip says the list is narrowed and leads back to the file.
    expect(screen.getByRole("link", { name: "From one file" })).toHaveAttribute("href", "/inbox/documents/d-7");
    // Other filters are applied on top of it.
    await user.type(screen.getByRole("searchbox", { name: "Search transactions" }), "market{Enter}");
    await waitFor(() => {
      const last = lastList(api);
      expect(last).toContain("q=market");
      expect(last).toContain("document=d-7");
    });
    await user.click(screen.getByRole("button", { name: "Remove the file filter" }));
    await waitFor(() => {
      const last = lastList(api);
      expect(last).not.toContain("document=");
      expect(last).toContain("q=market");
    });
    expect(currentHref(router)).toBe("/transactions?account=a-1&q=market");
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
  });

  it("Clear filters drops the file filter with the others", async () => {
    const api = setup();
    renderApp("/transactions?document=d-7&account=a-1");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    expect(screen.getByRole("link", { name: "From one file" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    await waitFor(() => {
      const last = lastList(api);
      expect(last).not.toContain("document=");
      expect(last).not.toContain("account=");
    });
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
  });

  it("reads the subscription filter from the address, sends it and drops it with its chip", async () => {
    const api = setup();
    renderApp("/transactions?subscription=s-1");
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /ACME/ });
    expect(api.callsTo("GET", "/api/v1/transactions")[0]?.search).toContain("subscription=s-1");
    expect(screen.getByRole("link", { name: "Charges of one subscription" })).toHaveAttribute("href", "/plan/subscriptions/s-1");
    await user.click(screen.getByRole("button", { name: "Remove the subscription filter" }));
    await waitFor(() => {
      expect(lastList(api)).not.toContain("subscription=");
    });
    expect(screen.queryByRole("link", { name: "Charges of one subscription" })).not.toBeInTheDocument();
  });

  it("shows no chip and sends no document or subscription without one in the address", async () => {
    const api = setup();
    renderApp("/transactions");
    await screen.findByRole("row", { name: /ACME/ });
    const first = api.callsTo("GET", "/api/v1/transactions")[0]?.search ?? "";
    expect(first).not.toContain("document=");
    expect(first).not.toContain("subscription=");
    expect(screen.queryByRole("link", { name: "From one file" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Charges of one subscription" })).not.toBeInTheDocument();
  });

  it("the dialog links to the source document", async () => {
    setup("owner", [tx("x-1", { source_document_id: "d-7" }), tx("x-2", { payee_id: null, raw_payee: "ACME" })]);
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Corner Market" }));
    await user.click(within(screen.getByRole("menu", { name: "Actions for Corner Market" })).getByRole("menuitem", { name: "Edit" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByRole("link", { name: "Imported from a document: open it" })).toHaveAttribute("href", "/inbox/documents/d-7?row=x-1");
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
