// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";

const STAMP = "2026-11-03T10:00:00Z";

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
        json({ transactions: 3, investment_transactions: 0, loan_payments: 0, balance_snapshots: 1, child_documents: 0 }),
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
        json({ transactions: 1, investment_transactions: 0, loan_payments: 0, balance_snapshots: 0, child_documents: 0 }),
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
