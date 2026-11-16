// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";

const STAMP = "2026-11-03T10:00:00Z";

function present<T>(value: T | null | undefined): T {
  if (value === null || value === undefined) {
    throw new Error("expected a value");
  }
  return value;
}

function row(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    account_id: null,
    created_at: STAMP,
    updated_at: STAMP,
    error: null,
    institution_id: null,
    kind: "statement",
    mime_detected: "application/pdf",
    original_filename: name,
    parser_version: null,
    period_end: null,
    period_start: null,
    sha256: "0".repeat(64),
    size: 2048,
    source: "upload",
    status: "processed",
    tax_year: null,
    uploaded_by: null,
    ...extra,
  };
}

const DETAIL = {
  ...row("d-1", "march.pdf"),
  can_edit: true,
  downloadable: true,
  links: [],
  open_review_items: 1,
  quarantined: false,
};

const REGIONS = {
  document_id: "d-1",
  page: 1,
  page_count: 2,
  width: 612,
  height: 792,
  unit: "pt",
  preview: null,
  text_included: true,
  text_source: "text_layer",
  words: [{ bbox: [10, 10, 60, 24], conf: 0.9, line: 1, text: "Rent" }],
  words_truncated: false,
};

describe("document library", () => {
  it("searches the text, filters by kind and shows the matched words as marks", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      "GET /api/v1/documents": (call) =>
        call.search.includes("q=rent")
          ? json({
              items: [
                row("d-1", "march.pdf", {
                  snippet: [
                    { text: "paid ", match: false },
                    { text: "rent", match: true },
                  ],
                }),
              ],
              next_cursor: null,
            })
          : json({ items: [row("d-1", "march.pdf"), row("d-2", "april.pdf")], next_cursor: null }),
    });
    renderApp("/documents/library");
    const user = userEvent.setup();
    expect(await screen.findByText("april.pdf")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Search the text of documents"), "rent");
    await user.selectOptions(screen.getByLabelText("Kind"), "statement");
    await user.click(screen.getByRole("button", { name: "Search" }));
    const hit = await screen.findByText("rent");
    expect(hit.tagName).toBe("MARK");
    expect(screen.queryByText("april.pdf")).not.toBeInTheDocument();
    const last = api.callsTo("GET", "/api/v1/documents").at(-1);
    expect(last?.search).toContain("q=rent");
    expect(last?.search).toContain("kind=statement");
  });

  it("shows a document with its page and a download link only when permitted", async () => {
    let downloadable = true;
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/documents/:id/regions": () => json(REGIONS),
      "GET /api/v1/documents/:id": () => json({ ...DETAIL, downloadable }),
    });
    const { unmount } = renderApp("/documents/d-1");
    const link = await screen.findByRole("link", { name: "Download original" });
    expect(link).toHaveAttribute("href", "/api/v1/documents/d-1/download");
    expect(await screen.findByText("Page 1 of 2")).toBeInTheDocument();
    unmount();
    downloadable = false;
    renderApp("/documents/d-1");
    await waitFor(() => {
      expect(screen.getByText("Your role cannot download the original file.")).toBeInTheDocument();
    });
    expect(screen.queryByRole("link", { name: "Download original" })).not.toBeInTheDocument();
  });

  it("deletes only the file from the detail page and returns to the library", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      "GET /api/v1/documents/:id/regions": () => json(REGIONS),
      "GET /api/v1/documents/:id/imported": () =>
        json({ transactions: 3, investment_transactions: 0, loan_payments: 0, balance_snapshots: 1, child_documents: 0, skipped_lines: 0, statements: [] }),
      "GET /api/v1/documents/:id": () => json(DETAIL),
      "DELETE /api/v1/documents/:id": () => new Response(null, { status: 204 }),
      "GET /api/v1/documents": () => json({ items: [], next_cursor: null }),
    });
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    expect(await screen.findByText(/3 transactions, 1 balance snapshot/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete file" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/documents/d-1")).toHaveLength(1);
    });
    expect(api.callsTo("DELETE", "/api/v1/documents/d-1")[0]?.search).toContain("with_data=false");
    expect(await screen.findByText("No documents yet.")).toBeInTheDocument();
  });

  it("deletes a document with its imported data from the library", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      "GET /api/v1/documents/:id/imported": () =>
        json({ transactions: 1, investment_transactions: 0, loan_payments: 0, balance_snapshots: 0, child_documents: 0, skipped_lines: 0, statements: [] }),
      "DELETE /api/v1/documents/:id": () => new Response(null, { status: 204 }),
      "GET /api/v1/documents": () => json({ items: [row("d-1", "march.pdf")], next_cursor: null }),
    });
    renderApp("/documents/library");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete march.pdf" }));
    await user.click(await screen.findByLabelText(/The file and everything imported from it/));
    await user.click(screen.getByRole("button", { name: "Delete file and data" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/documents/d-1")).toHaveLength(1);
    });
    expect(api.callsTo("DELETE", "/api/v1/documents/d-1")[0]?.search).toContain("with_data=true");
  });

  it("offers no delete to a viewer", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "viewer" }),
      "GET /api/v1/documents": () => json({ items: [row("d-1", "march.pdf")], next_cursor: null }),
    });
    renderApp("/documents/library");
    expect(await screen.findByText("march.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete march.pdf" })).not.toBeInTheDocument();
  });
});

describe("statement summary", () => {
  const SUMMARY = {
    document_id: "d-1",
    statements: [
      {
        index: 0,
        last4: "0001",
        name: "Individual",
        currency: "USD",
        period_start: "2026-10-01",
        period_end: "2026-10-31",
        as_of: "2026-10-31",
        positions: 3,
        activity: 12,
        positions_value: "4200.00",
        cash_opening: "500.00",
        cash_closing: "800.00",
        total_value: "5000.00",
        reconciliation: "mismatch",
        checks: ["positions", "holdings_total", "cash", "total_value"],
        failed: ["cash"],
        groups: [
          {
            group: "account_value",
            lines: [
              {
                label: "Beginning Account Value",
                role: "opening",
                detail: false,
                amount: "4500.00",
                year_to_date: "4000.00",
                page: 2,
              },
              {
                label: "Additions",
                role: "item",
                detail: false,
                amount: "250.00",
                year_to_date: null,
                page: 2,
              },
              {
                label: "Ending Account Value",
                role: "closing",
                detail: false,
                amount: "5000.00",
                year_to_date: "5000.00",
                page: 2,
              },
            ],
          },
        ],
        sections: [
          {
            label: "Total Deposits",
            rows: 2,
            status: "mismatch",
            printed: "250.00",
            computed: "200.00",
            difference: "-50.00",
            source: "total",
            page: 5,
          },
        ],
      },
    ],
  };

  function detailApi(summary: unknown, detail: Record<string, unknown> = {}) {
    return mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      "GET /api/v1/documents/:id": () => json({ ...DETAIL, ...detail }),
      "GET /api/v1/documents/:id/regions": () => json(REGIONS),
      "GET /api/v1/documents/:id/summary": () => json(summary),
    });
  }

  it("shows what the statement prints about itself, per account", async () => {
    detailApi(SUMMARY);
    renderApp("/documents/d-1");
    const account = await screen.findByRole("region", {
      name: "Individual ending in 0001",
    });
    expect(account).toHaveTextContent("Does not add up");
    expect(account).toHaveTextContent(
      "Does not hold: opening cash plus activity is the closing cash",
    );
    expect(account).toHaveTextContent(
      "Holds: holdings plus cash is the account value",
    );
    const value = screen.getByRole("table", { name: "Change in account value" });
    expect(value).toHaveTextContent("Beginning Account Value");
    expect(value).toHaveTextContent("$4,500.00");
    expect(value).toHaveTextContent("Year to date");
    const sections = screen.getByRole("table", {
      name: "Activity sections against their printed totals",
    });
    expect(sections).toHaveTextContent("Total Deposits");
    expect(sections).toHaveTextContent("$200.00");
    expect(sections).toHaveTextContent("Does not add up");
  });

  it("shows no card for a document without a brokerage statement", async () => {
    const api = detailApi({ document_id: "d-1", statements: [] });
    renderApp("/documents/d-1");
    await screen.findByRole("heading", { name: "march.pdf" });
    await waitFor(() => {
      expect(
        api.callsTo("GET", "/api/v1/documents/:id/summary"),
      ).toHaveLength(1);
    });
    expect(screen.queryByText("Statement summary")).not.toBeInTheDocument();
  });

  it("does not ask for the summary of a document the role cannot change", async () => {
    const api = detailApi(SUMMARY, { can_edit: false });
    renderApp("/documents/d-1");
    await screen.findByRole("heading", { name: "march.pdf" });
    expect(api.callsTo("GET", "/api/v1/documents/:id/summary")).toHaveLength(0);
    expect(screen.queryByText("Statement summary")).not.toBeInTheDocument();
  });
});

describe("what a file imported", () => {
  const COUNTS = {
    transactions: 14,
    investment_transactions: 0,
    loan_payments: 0,
    balance_snapshots: 1,
    child_documents: 0,
  };

  function imported(extra: Record<string, unknown> = {}) {
    return {
      account_id: "a-1",
      account_name: "Everyday",
      kind: "bank",
      period_start: "2026-10-01",
      period_end: "2026-10-31",
      outcome: "imported",
      verification: "unverified",
      source_format: "pdf",
      unchecked: true,
      transactions: 9,
      pending: 2,
      ...extra,
    };
  }

  const CHECKED = imported({
    account_id: "a-2",
    account_name: "Rainy day",
    verification: "reconciled",
    unchecked: false,
    transactions: 5,
    pending: 0,
  });

  function ledgerRow(id: string, payee: string) {
    return {
      transaction_id: id,
      posted_date: "2026-10-09",
      payee,
      amount: "-10.00",
      origin: "read",
      edited: false,
      in_period: true,
      review_state: "none",
      page: null,
      bbox: null,
    };
  }

  function reconciliation(index: number, accountId: string, rows: unknown[]) {
    return {
      statement: index,
      account_id: accountId,
      account_name: accountId === "a-1" ? "Everyday" : "Rainy day",
      can_edit: true,
      causes: [],
      opening: null,
      closing: null,
      previous_closing: null,
      currency: "USD",
      total: "-20.00",
      difference: null,
      lines_skipped: 0,
      period_start: "2026-10-01",
      period_end: "2026-10-31",
      printed_sign: "ledger",
      removed: [],
      rows,
      rows_truncated: false,
      status: "unchecked",
      verification: "unverified",
    };
  }

  function fileApi(
    statements: unknown[],
    detail: Record<string, unknown> = {},
    extra: Record<string, unknown> = {},
    role: "member" | "viewer" = "member",
  ) {
    return mockApi({
      "GET /api/v1/auth/session": authedSession({ role }),
      "GET /api/v1/documents/:id/regions": () => json(REGIONS),
      "GET /api/v1/documents/:id/summary": () =>
        json({ document_id: "d-1", statements: [] }),
      "GET /api/v1/documents/:id/imported": () =>
        json({ ...COUNTS, skipped_lines: 0, statements, ...extra }),
      "GET /api/v1/documents/:id/reconciliation": () =>
        json({
          document_id: "d-1",
          statements: [
            reconciliation(0, "a-2", [ledgerRow("t-9", "Ferry ticket")]),
            reconciliation(1, "a-1", [
              ledgerRow("t-1", "Harbour Grocer"),
              ledgerRow("t-2", "Tram pass"),
            ]),
          ],
        }),
      "POST /api/v1/documents/:id/reprocess": () =>
        json({ document_id: "d-1", job_id: "j-1", state: "queued" }, 202),
      "GET /api/v1/documents/:id": () => json({ ...DETAIL, ...detail }),
    });
  }

  it("lists what the file imported with its account and marker", async () => {
    fileApi([imported(), CHECKED], {}, { skipped_lines: 2 });
    renderApp("/documents/d-1");
    const card = within(
      await screen.findByRole("region", { name: "Imported from this file" }),
    );
    expect(card.getByRole("link", { name: "Everyday" })).toHaveAttribute(
      "href",
      "/accounts/a-1",
    );
    expect(card.getByRole("link", { name: "9 rows" })).toHaveAttribute(
      "href",
      "/transactions?document=d-1&account=a-1",
    );
    expect(card.getByRole("link", { name: "5 rows" })).toHaveAttribute(
      "href",
      "/transactions?document=d-1&account=a-2",
    );
    expect(card.getAllByText("2 waiting for review")).toHaveLength(1);
    expect(card.queryByText("0 waiting for review")).not.toBeInTheDocument();
    expect(card.getAllByText("Not checked against a balance")).toHaveLength(1);
    expect(card.getByText("Balanced against the statement")).toBeInTheDocument();
    expect(
      card.getAllByText(
        "This statement prints no balances PerFin could check its rows against. The rows read cleanly and were imported. You can enter the balances, or remove a row that does not belong.",
      ),
    ).toHaveLength(1);
    expect(card.getByText("2 line(s) were not read")).toBeInTheDocument();
  });

  it("says a statement was accepted and marks nothing that was never checkable", async () => {
    fileApi([
      imported({ verification: "accepted", unchecked: false }),
      imported({
        account_id: "a-2",
        account_name: "Rainy day",
        source_format: "csv",
        unchecked: false,
      }),
    ]);
    renderApp("/documents/d-1");
    const card = within(
      await screen.findByRole("region", { name: "Imported from this file" }),
    );
    expect(card.getByText("Accepted without balancing")).toBeInTheDocument();
    expect(
      card.queryByText("Not checked against a balance"),
    ).not.toBeInTheDocument();
    expect(card.queryByText(/prints no balances/)).not.toBeInTheDocument();
    expect(card.queryByText(/were not read/)).not.toBeInTheDocument();
    // Accepted can still be balanced; a CSV has nothing to balance against.
    expect(
      card.getAllByRole("button", { name: "Check against the statement" }),
    ).toHaveLength(1);
  });

  it("offers the reconcile view on an unchecked statement and not to a viewer", async () => {
    const api = fileApi([imported(), CHECKED]);
    const { unmount } = renderApp("/documents/d-1");
    const user = userEvent.setup();
    const card = within(
      await screen.findByRole("region", { name: "Imported from this file" }),
    );
    // Only the statement that was not balanced offers it.
    const open = card.getAllByRole("button", {
      name: "Check against the statement",
    });
    expect(open).toHaveLength(1);
    expect(
      api.callsTo("GET", "/api/v1/documents/:id/reconciliation"),
    ).toHaveLength(0);
    await user.click(present(open[0]));
    const rows = within(
      await screen.findByRole("table", {
        name: "Rows of this statement in PerFin",
      }),
    );
    // The statement of this account, not the first one of the document.
    expect(rows.getByText("Harbour Grocer")).toBeInTheDocument();
    expect(rows.queryByText("Ferry ticket")).not.toBeInTheDocument();
    unmount();

    fileApi([imported(), CHECKED], { can_edit: false }, {}, "viewer");
    renderApp("/documents/d-1");
    const readOnly = within(
      await screen.findByRole("region", { name: "Imported from this file" }),
    );
    expect(
      readOnly.getByText("Not checked against a balance"),
    ).toBeInTheDocument();
    expect(
      readOnly.queryByRole("button", { name: "Check against the statement" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Process again" }),
    ).not.toBeInTheDocument();
  });

  it("brings the row named in the address into view when the view is opened", async () => {
    const scrolled: Element[] = [];
    const original = Object.getOwnPropertyDescriptor(
      Element.prototype,
      "scrollIntoView",
    );
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value(this: Element) {
        scrolled.push(this);
      },
    });
    try {
      fileApi([imported(), CHECKED]);
      renderApp("/documents/d-1?row=t-2");
      const user = userEvent.setup();
      await user.click(
        await screen.findByRole("button", {
          name: "Check against the statement",
        }),
      );
      await waitFor(() => {
        expect(scrolled).toHaveLength(1);
      });
      expect(scrolled[0]?.tagName).toBe("TR");
      expect(scrolled[0]).toHaveTextContent("Tram pass");
      expect(scrolled[0]).not.toHaveTextContent("Harbour Grocer");
    } finally {
      if (original === undefined) {
        Reflect.deleteProperty(Element.prototype, "scrollIntoView");
      } else {
        Object.defineProperty(Element.prototype, "scrollIntoView", original);
      }
    }
  });

  it("hides Teach PerFin for a file with no pages", async () => {
    fileApi([], { previewable: false });
    const { unmount } = renderApp("/documents/d-1");
    expect(
      await screen.findByRole("button", { name: "Process again" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Teach PerFin" }),
    ).not.toBeInTheDocument();
    // Nothing was imported, so there is no card about it.
    expect(
      screen.queryByRole("region", { name: "Imported from this file" }),
    ).not.toBeInTheDocument();
    unmount();

    fileApi([], { previewable: true });
    renderApp("/documents/d-1");
    expect(
      await screen.findByRole("button", { name: "Teach PerFin" }),
    ).toBeInTheDocument();
  });

  it("links its open questions to the filtered queue", async () => {
    fileApi([imported()], { open_review_items: 3 });
    renderApp("/documents/d-1");
    expect(
      await screen.findByRole("link", { name: "3 item(s) to review" }),
    ).toHaveAttribute("href", "/review?document=d-1");
  });

  it("process again posts reprocess", async () => {
    const api = fileApi([imported()]);
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Process again" }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/documents/d-1/reprocess"),
      ).toHaveLength(1);
    });
    expect(await screen.findByText("Reading the file again")).toBeInTheDocument();
  });

  it("says why when the file cannot be processed again", async () => {
    const api = fileApi([imported()]);
    api.on("POST /api/v1/documents/:id/reprocess", () =>
      json({ title: "The file is still being read", status: 409 }, 409),
    );
    renderApp("/documents/d-1");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Process again" }),
    );
    expect(
      await screen.findByText("Could not process the file again"),
    ).toBeInTheDocument();
  });
});
