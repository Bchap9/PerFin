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
});
