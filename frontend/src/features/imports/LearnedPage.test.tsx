// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, renderApp } from "../auth/testing";
import type { CategorisationSummary, LearnedCategory } from "../categories/api";
import { lookupHandlers, NOW, summary } from "../transactions/fixtures";
import type { LearnedLayout, LearnedWording, Mapping } from "./api";
import { layoutReads, mappingReads, sourceCountLine, timesText, wordingMeans } from "./learned";

const TAUGHT: LearnedLayout = {
  id: "l-1",
  institution_id: "i-1",
  institution_name: "Quillfeather Mutual",
  kind: "statement",
  parser: "statement_generic",
  version: 2,
  learned_at: NOW,
  learned_from_document_id: "d-1",
  recognised_by: ["quillfeather mutual"],
  taught: true,
  fields: ["closing_balance", "period_end", "period_start"],
  tables: [{ kind: "transactions", columns: ["date", "payee", "amount"] }],
  ignored_lines: 1,
  notes: ["QF is the branch code"],
};

const NAMED: LearnedLayout = {
  ...TAUGHT,
  id: "l-2",
  institution_name: "Sample Card Company",
  version: 1,
  learned_from_document_id: null,
  recognised_by: ["sample card company"],
  taught: false,
  fields: [],
  tables: [],
  ignored_lines: 0,
  notes: [],
};

const MAPPING: Mapping = {
  id: "m-1",
  name: "Everyday export",
  account_id: "a-1",
  institution_id: null,
  header_fingerprint: "f".repeat(64),
  created_at: NOW,
  updated_at: NOW,
  created_by: null,
  last_used_at: NOW,
  use_count: 3,
  spec: {
    version: 1,
    skip_rows: 3,
    has_header: true,
    date_column: 0,
    date_format: "DD.MM.YYYY",
    debit_column: 5,
    credit_column: 6,
    decimal_separator: ",",
    sign: "inflow_positive",
    mode: "cash",
  },
};

/** Three of three corrections went one way and no rule covers the merchant yet: a rule is offered. */
const UNANIMOUS: LearnedCategory = {
  id: "m-1",
  merchant: "Quiet Bean Coffee",
  merchant_key: "quiet bean coffee",
  direction: "out",
  category_id: "c-2",
  decisions: 3,
  total: 3,
  share: "1.00",
  last_decided_at: NOW,
  offer_rule: true,
  rule_id: null,
  example_transaction_id: "t-7",
};
const DIVIDED: LearnedCategory = {
  ...UNANIMOUS,
  id: "m-2",
  merchant: "Corner Grocer",
  merchant_key: "corner grocer",
  category_id: "c-1",
  decisions: 2,
  share: "0.67",
  offer_rule: false,
  example_transaction_id: "t-8",
};

const COUNTED: CategorisationSummary = summary({
  by_source: [
    { source: "dictionary", transactions: 120 },
    { source: "person", transactions: 14 },
    { source: "keyword", transactions: 1 },
    { source: null, transactions: 9 },
  ],
  dictionary_version: 3,
  dictionary_merchants: 950,
  learned_merchants: 2,
  uncategorised: 9,
  suggestions: 4,
  up_to_date: false,
});

function handlers(role: "owner" | "member" | "viewer" = "owner") {
  return {
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/categorisation/summary": () => json(COUNTED),
    "GET /api/v1/categorisation/learned": () => json({ items: [UNANIMOUS, DIVIDED], next_cursor: null }),
    "GET /api/v1/imports/layouts": () => json({ items: [TAUGHT, NAMED] }),
    "GET /api/v1/imports/wording": () => json({ items: [WORDING, HEADING] }),
    "GET /api/v1/imports/mappings": () => json({ items: [MAPPING], next_cursor: null }),
  };
}

const WORDING: LearnedWording = {
  id: "a".repeat(32),
  layout: "fidelity-investments-statement",
  kind: "action",
  phrase: "periodic investment plan",
  meaning: "buy",
  learned_at: "2026-11-11T09:00:00Z",
  learned_from_document_id: "d-9",
};
const HEADING: LearnedWording = {
  id: "b".repeat(32),
  layout: "brokerage_statement_generic",
  kind: "section",
  phrase: "sweep program activity",
  meaning: "cash",
  learned_at: "2026-11-10T09:00:00Z",
  learned_from_document_id: null,
};

describe("what a learned layout and a mapping say they read", () => {
  it("names labelled facts, table columns and ignored lines in plain words", () => {
    expect(layoutReads(TAUGHT)).toEqual([
      "Labelled: closing balance, statement end, statement start.",
      "Transactions table: date, description, amount.",
      "1 line ignored.",
    ]);
    expect(layoutReads(NAMED)).toEqual(["Recognises the institution; the usual readers do the rest."]);
  });

  it("describes a mapping's dates, money columns and preamble", () => {
    expect(mappingReads(MAPPING)).toBe(
      "Reads dates as DD.MM.YYYY, separate money in and money out columns, decimal comma, 3 lines skipped above the header.",
    );
  });
});

describe("LearnedPage", () => {
  it("lists layouts and mappings with what they do", async () => {
    mockApi(handlers());
    renderApp("/settings/learned");
    const layouts = await screen.findByRole("table", { name: "Learned statement layouts" });
    expect(within(layouts).getByText("Quillfeather Mutual")).toBeInTheDocument();
    expect(within(layouts).getByText("Taught")).toBeInTheDocument();
    expect(within(layouts).getByText("Recognised")).toBeInTheDocument();
    expect(within(layouts).getByText('"quillfeather mutual"')).toBeInTheDocument();
    expect(within(layouts).getByText("Note: QF is the branch code")).toBeInTheDocument();
    const mappings = await screen.findByRole("table", { name: "Saved column mappings" });
    expect(within(mappings).getByText("Everyday export")).toBeInTheDocument();
    expect(within(mappings).getByText(/3 times, last/)).toBeInTheDocument();
  });

  it("lists the wording it learned in plain words and forgets a phrase", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/wording/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/learned");
    const table = await screen.findByRole("table", { name: "Learned statement wording" });
    expect(within(table).getByText('"periodic investment plan"')).toBeInTheDocument();
    expect(within(table).getByText("A line that is a purchase")).toBeInTheDocument();
    expect(within(table).getByText("fidelity investments statement")).toBeInTheDocument();
    expect(
      within(table).getByText("A section of cash in or out, by the sign of the amount"),
    ).toBeInTheDocument();
    expect(screen.getByText(/No amount, date, security,\s+account or name from the statement is stored/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: 'Forget "periodic investment plan"' }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", `/api/v1/imports/wording/${"a".repeat(32)}`)).toHaveLength(1);
    });
  });

  it("forgets a layout after a confirmation", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/layouts/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/learned");
    await user.click(await screen.findByRole("button", { name: "Forget the Quillfeather Mutual layout" }));
    const dialog = await screen.findByRole("dialog", { name: "Forget the Quillfeather Mutual layout?" });
    expect(api.callsTo("DELETE", "/api/v1/imports/layouts/:id")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Forget this layout" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/imports/layouts/l-1")).toHaveLength(1);
    });
    const [call] = api.callsTo("DELETE", "/api/v1/imports/layouts/l-1");
    expect(call?.headers.get("X-CSRF-Token")).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("shows the server's refusal and keeps the dialog open", async () => {
    const user = userEvent.setup();
    mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/layouts/:id": () =>
        json({ type: "layout-not-found", title: "Not found", status: 404, detail: "No such learned layout." }, 404),
    });
    renderApp("/settings/learned");
    await user.click(await screen.findByRole("button", { name: "Forget the Sample Card Company layout" }));
    await user.click(screen.getByRole("button", { name: "Forget this layout" }));
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No such learned layout.");
    expect(dialog).toBeInTheDocument();
  });

  it("renames and deletes a column mapping", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "PATCH /api/v1/imports/mappings/:id": (call) => json({ ...MAPPING, ...(call.body as object) }),
      "DELETE /api/v1/imports/mappings/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/learned");
    await user.click(await screen.findByRole("button", { name: "Rename Everyday export" }));
    const name = screen.getByRole("textbox", { name: /Name/ });
    await user.clear(name);
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
    await user.type(name, "Joint checking export");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/imports/mappings/m-1")).toHaveLength(1);
    });
    const [patch] = api.callsTo("PATCH", "/api/v1/imports/mappings/m-1");
    expect(patch?.body).toEqual({ name: "Joint checking export" });
    expect(patch?.headers.get("X-CSRF-Token")).toBeTruthy();

    await user.click(await screen.findByRole("button", { name: "Delete Everyday export" }));
    await user.click(screen.getByRole("button", { name: "Delete mapping" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/imports/mappings/m-1")).toHaveLength(1);
    });
  });

  it("Make it a rule opens the drawer link", async () => {
    const api = mockApi(handlers("member"));
    const view = renderApp("/settings/learned");
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Categories learned from you" });
    // Following it lands on the transactions page with that row's drawer asked for; the
    // "Always use" choice is made there, so nothing is saved by following the link.
    await user.click(within(table).getByRole("link", { name: "Make Quiet Bean Coffee a rule" }));
    await waitFor(() => {
      expect(currentHref(view.router)).toBe("/transactions?always=t-7");
    });
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(0);
    expect(api.callsTo("POST", "/api/v1/rules")).toHaveLength(0);
  });

  it("says where rules are made and links to its documents in the Inbox", async () => {
    mockApi(handlers());
    renderApp("/settings/learned");
    expect(
      await screen.findByText(
        "Rules are made where you work: in a transaction's panel or from an Inbox card. Here you can change their order, edit or undo them.",
      ),
    ).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumbs" });
    expect(within(crumbs).getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("heading", { level: 2, name: "What PerFin learned" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "rules" })).toHaveAttribute("href", "/settings/rules");
    await screen.findByRole("table", { name: "Learned statement wording" });
    const documentLinks = screen
      .getAllByRole("link")
      .map((link) => link.getAttribute("href") ?? "")
      .filter((href) => href.includes("/documents/"));
    expect(documentLinks.length).toBeGreaterThan(0);
    for (const href of documentLinks) {
      expect(href).toMatch(/^\/inbox\/documents\/[^/]+$/);
    }
  });

  it("has no axe violations", async () => {
    mockApi(handlers());
    const { container } = renderApp("/settings/learned");
    await screen.findByRole("table", { name: "Categories learned from you" });
    await screen.findByText("Everyday export");
    await expectNoAxeViolations(container);
  });

  it("gives a viewer the lists without the buttons", async () => {
    mockApi(handlers("viewer"));
    renderApp("/settings/learned");
    await screen.findByRole("table", { name: "Learned statement layouts" });
    await screen.findByText("Everyday export");
    await screen.findByText("Quiet Bean Coffee");
    expect(screen.queryByRole("button", { name: /Forget/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Rename|Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check again" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /a rule$/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Here you can change their order/)).not.toBeInTheDocument();
    expect(screen.getByText(/Only an owner or admin can change their order or edit them\.$/)).toBeInTheDocument();
  });
});

describe("LearnedPage: categories", () => {
  it("lists what was learned and forgets an entry", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers("member"),
      "DELETE /api/v1/categorisation/learned/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/settings/learned");
    const table = await screen.findByRole("table", { name: "Categories learned from you" });
    const unanimous = within(table).getByRole("rowheader", { name: "Quiet Bean Coffee" }).closest("tr");
    expect(unanimous).not.toBeNull();
    expect(await within(unanimous as HTMLElement).findByText("Dining")).toBeInTheDocument();
    expect(within(unanimous as HTMLElement).getByText("3 of 3 times")).toBeInTheDocument();
    const divided = within(table).getByRole("rowheader", { name: "Corner Grocer" }).closest("tr");
    expect(within(divided as HTMLElement).getByText("Groceries")).toBeInTheDocument();
    expect(within(divided as HTMLElement).getByText("2 of 3 times")).toBeInTheDocument();
    // What the bank printed is not on this page: only the merchant's name.
    expect(screen.queryByText("quiet bean coffee")).not.toBeInTheDocument();

    api.on("GET /api/v1/categorisation/learned", () => json({ items: [DIVIDED], next_cursor: null }));
    await user.click(within(table).getByRole("button", { name: "Forget what was learned about Quiet Bean Coffee" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/categorisation/learned/m-1")).toHaveLength(1);
    });
    expect(api.callsTo("DELETE", "/api/v1/categorisation/learned/m-1")[0]?.headers.get("X-CSRF-Token")).toBeTruthy();
    await waitFor(() => {
      expect(within(table).queryByText("Quiet Bean Coffee")).not.toBeInTheDocument();
    });
    expect(within(table).getByText("Corner Grocer")).toBeInTheDocument();
  });

  it("offers a rule at three unanimous decisions", async () => {
    mockApi(handlers("member"));
    renderApp("/settings/learned");
    const table = await screen.findByRole("table", { name: "Categories learned from you" });
    // The link opens "Always categorise..." for a row of that merchant on the transactions page.
    expect(within(table).getByRole("link", { name: "Make Quiet Bean Coffee a rule" })).toHaveAttribute(
      "href",
      "/transactions?always=t-7",
    );
    expect(within(table).queryByRole("link", { name: "Make Corner Grocer a rule" })).not.toBeInTheDocument();
  });

  it("offers no rule without a transaction to make it from, and names the rule a merchant already has", async () => {
    mockApi({
      ...handlers("member"),
      "GET /api/v1/categorisation/learned": () =>
        json({
          items: [
            { ...UNANIMOUS, example_transaction_id: null },
            { ...DIVIDED, decisions: 3, share: "1.00", rule_id: "r-4" },
          ],
          next_cursor: null,
        }),
    });
    renderApp("/settings/learned");
    const table = await screen.findByRole("table", { name: "Categories learned from you" });
    expect(within(table).queryByRole("link", { name: /a rule$/ })).not.toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "The rule for Corner Grocer" })).toHaveAttribute("href", "/settings/rules/r-4");
  });

  it("shows the counts by source and the dictionary version", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers("owner"),
      "POST /api/v1/categorisation/refresh": () => json({ job_id: "j-5", state: "queued" }, 202),
      "GET /api/v1/jobs/:id": () =>
        json({ id: "j-5", type: "categorise.refresh", state: "running", finished: false, attempts: 1, max_attempts: 3, created_at: NOW, updated_at: NOW }),
    });
    renderApp("/settings/learned");
    const list = await screen.findByRole("list", { name: "Transactions by how their category was decided" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "You chose: 14 transactions",
      "Merchant list: 120 transactions",
      "Keyword: 1 transaction",
      "Not decided: 9 transactions",
    ]);
    expect(screen.getByText("Merchant list version 3, with 950 merchants.")).toBeInTheDocument();
    expect(screen.getByText("Not up to date yet")).toBeInTheDocument();
    expect(screen.getByText("9 transactions have no category; 4 of them have a suggestion.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/categorisation/refresh")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/categorisation/refresh")[0]?.headers.get("X-CSRF-Token")).toBeTruthy();
    expect(await screen.findByText("Running")).toBeInTheDocument();
  });

  it("says nothing about being behind when it is up to date", async () => {
    mockApi({ ...handlers(), "GET /api/v1/categorisation/summary": () => json({ ...COUNTED, up_to_date: true }) });
    renderApp("/settings/learned");
    await screen.findByText("Merchant list version 3, with 950 merchants.");
    expect(screen.queryByText("Not up to date yet")).not.toBeInTheDocument();
  });

  it('a member sees no "Check again"', async () => {
    mockApi(handlers("member"));
    renderApp("/settings/learned");
    await screen.findByText("Merchant list version 3, with 950 merchants.");
    expect(await screen.findByRole("button", { name: "Forget what was learned about Quiet Bean Coffee" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Check again" })).not.toBeInTheDocument();
  });

  it("names the three ways a category is decided", async () => {
    mockApi(handlers());
    renderApp("/settings/learned");
    expect(
      await screen.findByText(/what PerFin learned from the categories you chose, and the merchant list that ships with PerFin/),
    ).toBeInTheDocument();
  });
});

describe("learned categories in words", () => {
  it("counts decisions and transactions in the singular and the plural", () => {
    expect(timesText(1, 1)).toBe("1 of 1 time");
    expect(timesText(2, 3)).toBe("2 of 3 times");
    expect(sourceCountLine({ source: "learned", transactions: 1 })).toBe("Learned from you: 1 transaction");
    expect(sourceCountLine({ source: "legacy", transactions: 40 })).toBe("From before this version: 40 transactions");
    expect(sourceCountLine({ source: "rule", transactions: 2 })).toBe("A rule: 2 transactions");
  });
});

describe("what a learned phrase means", () => {
  const base = {
    id: "w",
    layout: "example-brokerage-statement",
    learned_at: "2026-11-05T00:00:00Z",
    learned_from_document_id: null,
  };

  it("says what a column heading holds", () => {
    expect(
      wordingMeans({ ...base, kind: "column", phrase: "money in out", meaning: "amount" }),
    ).toBe("A table column holding the amount");
    expect(
      wordingMeans({ ...base, kind: "section", phrase: "sweep program activity", meaning: "cash" }),
    ).toContain("A section of cash in or out");
    expect(
      wordingMeans({ ...base, kind: "action", phrase: "periodic investment plan", meaning: "buy" }),
    ).toBe("A line that is a purchase");
  });
});
