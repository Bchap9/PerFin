// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp, type Handler } from "../auth/testing";
import { account, lookupHandlers, NOW } from "../transactions/fixtures";
import type { ReviewItem } from "./api";
import {
  IGNORE_LABEL,
  OTHER_LABEL,
  SELECTION_CHOICES,
  columnsFromWords,
  newSelection,
  problems,
  rolesForTable,
  teachBody,
  type PageWord,
  type TeachRole,
} from "./teach";

const WORDS: PageWord[] = [
  { bbox: [60, 40, 110, 50], line: 0, text: "Carried" },
  { bbox: [114, 40, 130, 50], line: 0, text: "out" },
  { bbox: [140, 40, 190, 50], line: 0, text: "921.93" },
  { bbox: [60, 80, 100, 90], line: 1, text: "Booked" },
  { bbox: [150, 80, 210, 90], line: 1, text: "Narrative" },
  { bbox: [400, 80, 450, 90], line: 1, text: "Movement" },
  { bbox: [60, 100, 120, 110], line: 2, text: "2026-04-07" },
  { bbox: [150, 100, 200, 110], line: 2, text: "Grocer" },
  { bbox: [410, 100, 450, 110], line: 2, text: "-62.57" },
];

function item(extra: Partial<ReviewItem> = {}): ReviewItem {
  return {
    id: "r-1",
    kind: "unparsed",
    state: "open",
    document_id: "d-1",
    document: {
      id: "d-1",
      original_filename: "ledger.pdf",
      kind: "unknown",
      status: "needs_review",
      mime_detected: "application/pdf",
      source: "upload",
      created_at: NOW,
      period_start: null,
      period_end: null,
      previewable: true,
    },
    reason: "no_records",
    payload: { reason: "no_records" },
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: "reprocess",
    can_resolve: true,
    ...extra,
  };
}

const PAGE = {
  document_id: "d-1",
  page: 1,
  page_count: 1,
  width: 612,
  height: 792,
  unit: "pt",
  preview: null,
  text_included: true,
  text_source: "text_layer",
  words: WORDS.map((w) => ({ ...w, conf: null })),
  words_truncated: false,
};

function reviewApi(detail: ReviewItem, extra: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner" }),
    ...lookupHandlers([account("a-1", "Everyday")]),
    "GET /api/v1/review": () => json({ items: [detail], next_cursor: null }),
    "GET /api/v1/review/:id": () => json(detail),
    "GET /api/v1/documents/:id/regions": () => json(PAGE),
    ...extra,
  });
}

describe("teach helpers", () => {
  it("always offers Other and Ignore, for selections and for every column", () => {
    const labels = SELECTION_CHOICES.map((c) => c.label);
    expect(labels).toContain(OTHER_LABEL);
    expect(labels).toContain(IGNORE_LABEL);
    for (const kind of ["transactions", "holdings"] as const) {
      const roles = rolesForTable(kind).map((r) => r.label);
      expect(roles).toContain(OTHER_LABEL);
      expect(roles).toContain(IGNORE_LABEL);
    }
    expect(rolesForTable("transactions").map((r) => r.value)).toContain("debit");
    expect(rolesForTable("holdings").map((r) => r.value)).toContain("market_value");
  });

  it("splits a selected table into columns from its header line", () => {
    const columns = columnsFromWords(WORDS, [50, 75, 500, 115]);
    expect(columns.map((c) => c.header)).toEqual(["Booked", "Narrative", "Movement"]);
    expect(columns[0]?.x0).toBe(50);
    expect(columns[2]?.x1).toBe(500);
    expect(columns[0]?.x1).toBe(125);
  });

  it("builds the request and lists what is missing", () => {
    const table = { ...newSelection(1, [50, 75, 500, 115], WORDS), kind: "transactions" as const };
    expect(problems([table])).toHaveLength(3);
    const roles: readonly TeachRole[] = ["date", "other", "amount"];
    const ready = {
      ...table,
      columns: table.columns.map((c, i) => ({
        ...c,
        role: roles.at(i) ?? "ignore",
        note: i === 1 ? "Who was paid" : "",
      })),
    };
    expect(problems([ready])).toEqual([]);
    const body = teachBody([ready], {
      institutionName: " Quill Society ",
      remember: true,
      reprocessSimilar: false,
      itemId: null,
    });
    expect(body).toEqual({
      labels: [],
      tables: [
        {
          kind: "transactions",
          page: 1,
          bbox: [50, 75, 500, 115],
          columns: [
            { x0: 50, x1: 125, role: "date" },
            { x0: 125, x1: 305, role: "other", note: "Who was paid" },
            { x0: 305, x1: 500, role: "amount" },
          ],
        },
      ],
      remember: true,
      reprocess_similar: false,
      resolve: false,
      institution_name: "Quill Society",
    });
  });
});

describe("teach dialog", () => {
  it("labels a selection, describes another with free text and posts the labels", async () => {
    const api = reviewApi(item(), {
      "POST /api/v1/documents/:id/teach": () =>
        json({
          document_id: "d-1",
          fields: [],
          tables: [],
          ignored: 0,
          notes: ["The society's branch code"],
          applied: [],
          warnings: ["no_label:closing_balance"],
          reprocessed: [],
        }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Teach PerFin" }));
    const dialog = await screen.findByRole("dialog", { name: "Teach PerFin" });
    const lines = await within(dialog).findByLabelText("Choose a line");
    await user.selectOptions(lines, "0");
    const what = within(dialog).getByLabelText("What is this?");
    const offered = within(what)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(offered).toContain(OTHER_LABEL);
    expect(offered).toContain(IGNORE_LABEL);
    await user.selectOptions(what, "closing_balance");

    await user.selectOptions(within(dialog).getByLabelText("Choose a line"), "1");
    const second = within(dialog).getAllByLabelText("What is this?")[1];
    expect(second).toBeDefined();
    if (second === undefined) {
      return;
    }
    expect(within(dialog).queryByLabelText("Describe it")).toBeNull();
    await user.selectOptions(second, "other");
    await user.type(within(dialog).getByLabelText("Describe it"), "The society's branch code");
    expect(within(dialog).getAllByTestId("selection-box")).toHaveLength(2);

    await user.click(within(dialog).getByRole("button", { name: "Apply to this file" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/documents/:id/teach")[0]?.body).toEqual({
        labels: [
          { field: "closing_balance", page: 1, bbox: [60, 40, 190, 50] },
          {
            field: "other",
            page: 1,
            bbox: [60, 80, 450, 90],
            note: "The society's branch code",
          },
        ],
        tables: [],
        remember: false,
        reprocess_similar: true,
        resolve: true,
        item_id: "r-1",
      });
    });
    expect(
      await within(dialog).findByText(/No label is printed next to the closing balance/),
    ).toBeInTheDocument();
  });

  it("enters the cost basis of an opening position", async () => {
    const detail = item({
      kind: "low_confidence_field",
      reason: "investment_opening_basis_unknown",
      payload: {
        reason: "investment_opening_basis_unknown",
        account_id: "a-1",
        securities: [
          {
            security_id: "s-1",
            quantity: "12",
            trade_date: "2026-03-02",
            transaction_id: "it-1",
          },
        ],
      },
      next_step: "correct_fields",
    });
    const api = reviewApi(detail, {
      "PATCH /api/v1/investments/transactions/:id": () => json({ id: "it-1" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const price = await screen.findByLabelText("Cost per unit");
    const save = screen.getByRole("button", { name: "Save cost basis" });
    expect(save).toBeDisabled();
    await user.type(price, "41.25");
    await user.click(save);
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/investments/transactions/:id")[0]?.body).toEqual({
        price: "41.25",
      });
    });
    expect(api.callsTo("PATCH", "/api/v1/investments/transactions/:id")[0]?.path).toBe(
      "/api/v1/investments/transactions/it-1",
    );
  });

  it("enters the balances of a statement that does not add up", async () => {
    const detail = item({
      kind: "low_confidence_field",
      reason: "reconciliation_failed",
      payload: {
        reason: "reconciliation_failed",
        account_id: "a-1",
        opening: "100.00",
        closing: "150.00",
        difference: "-10.00",
      },
      next_step: "correct_fields",
    });
    const api = reviewApi(detail, {
      "POST /api/v1/review/:id/correct": () => json({ ...detail, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const closing = await screen.findByLabelText("Closing balance");
    expect(closing).toHaveValue("150.00");
    await user.clear(closing);
    await user.type(closing, "140.00");
    await user.click(screen.getByRole("button", { name: "Check and accept" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/correct")[0]?.body).toEqual({
        document: { opening_balance: "100.00", closing_balance: "140.00" },
        resolve: true,
      });
    });
  });
});
