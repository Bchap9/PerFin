// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  authedSession,
  json,
  mockApi,
  problem,
  renderApp,
  type Handler,
} from "../auth/testing";
import { account, lookupHandlers, NOW, tx } from "../transactions/fixtures";
import {
  fixFromAlternative,
  institutionChoices,
  layoutRequest,
  lowConfidenceResolution,
  lineFixResolution,
  mergeFix,
  ownerResolution,
  type ReviewCard,
  type ReviewItem,
} from "./api";

function item(
  id: string,
  kind: ReviewItem["kind"],
  extra: Partial<ReviewItem> = {},
): ReviewItem {
  return {
    id,
    kind,
    state: "open",
    document_id: "d-1",
    payload: {},
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: null,
    can_resolve: true,
    ...extra,
  };
}

const PAGE = {
  document_id: "d-1",
  page: 1,
  page_count: 2,
  width: 612,
  height: 792,
  unit: "pt",
  preview: null,
  text_included: true,
  text_source: "text_layer",
  words: [{ bbox: [10, 10, 40, 20], conf: 0.9, line: 1, text: "X" }],
  words_truncated: false,
};

const ACTIONS: ReadonlyMap<ReviewItem["kind"], ReviewCard["action"]> = new Map([
  ["low_confidence_field", "check_rows"],
  ["unknown_owner", "choose_owner"],
  ["receipt_match", "attach_receipt"],
]);

/** The item as the one card of its document, with the action the server names. */
function card(detail: ReviewItem): ReviewCard {
  return {
    document: {
      id: "d-1",
      original_filename: "scan.pdf",
      kind: "statement",
      status: "needs_review",
      mime_detected: "application/pdf",
      source: "upload",
      created_at: NOW,
      period_start: null,
      period_end: null,
      previewable: true,
    },
    items: [detail],
    action: ACTIONS.get(detail.kind) ?? "acknowledge",
    action_item_id: detail.id,
    open: 1,
    notices: 0,
  };
}

function toolsApi(detail: ReviewItem, extra: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner" }),
    ...lookupHandlers([account("a-1", "Everyday")]),
    "GET /api/v1/review/cards": () =>
      json({ cards: [card(detail)], loose: [], next_cursor: null }),
    "GET /api/v1/review/:id": () => json(detail),
    "GET /api/v1/documents/:id/regions": () => json(PAGE),
    "GET /api/v1/documents/:id": () =>
      json({
        id: "d-1",
        original_filename: "scan.pdf",
        mime_detected: "application/pdf",
      }),
    // A row a review item lists waits for its confirmation.
    "GET /api/v1/transactions/:id": () =>
      json(tx("t-1", { review_state: "pending" })),
    ...extra,
  });
}

describe("correction helpers", () => {
  it("keeps the row's sign when taking an unsigned amount from the page", () => {
    const current = { amount: { amount: "-42.10", currency: "USD" } };
    expect(
      fixFromAlternative(
        {
          field: "amount",
          value: "24.10",
          source: "document",
          transaction_id: "t-1",
        },
        current,
      ),
    ).toEqual({
      transaction_id: "t-1",
      amount: { amount: "-24.10", currency: "USD" },
    });
    expect(
      fixFromAlternative(
        {
          field: "posted_date",
          value: "2026-10-02",
          source: "document",
          transaction_id: "t-1",
        },
        current,
      ),
    ).toEqual({ transaction_id: "t-1", posted_date: "2026-10-02" });
    expect(
      fixFromAlternative(
        { field: "amount", value: "1", source: "document" },
        current,
      ),
    ).toBeNull();
  });

  it("merges fixes per row and builds the other bodies", () => {
    const merged = mergeFix(
      mergeFix([], { transaction_id: "t-1", posted_date: "2026-10-02" }),
      { transaction_id: "t-1", amount: { amount: "1.00", currency: "USD" } },
    );
    expect(merged).toEqual([
      {
        transaction_id: "t-1",
        posted_date: "2026-10-02",
        amount: { amount: "1.00", currency: "USD" },
      },
    ]);
    expect(lowConfidenceResolution(merged, true).kind).toBe(
      "low_confidence_field",
    );
    expect(layoutRequest("i-1", false)).toEqual({
      institution_id: "i-1",
      reprocess_similar: false,
      replace: false,
    });
    expect(layoutRequest("i-1", true, true)).toEqual({
      institution_id: "i-1",
      reprocess_similar: true,
      replace: true,
    });
    expect(lineFixResolution([2], [{ line: 2, type: "fee" }])).toEqual({
      kind: "low_confidence_field",
      transactions: [],
      confirm: false,
      import_as_read: true,
      lines: [2],
      line_fixes: [{ line: 2, type: "fee" }],
    });
    expect(ownerResolution("u-1", "", true)).toEqual({
      kind: "unknown_owner",
      user_id: "u-1",
      remember: true,
    });
    expect(ownerResolution("u-1", "a-1", false)).toEqual({
      kind: "unknown_owner",
      user_id: "u-1",
      account_id: "a-1",
      remember: false,
    });
    expect(
      institutionChoices(
        item("r", "new_institution", {
          alternatives: [
            {
              field: "institution_id",
              value: "i-1",
              label: "Bank A",
              source: "household",
            },
            { field: "amount", value: "1", source: "document" },
          ],
        }),
      ),
    ).toEqual([{ id: "i-1", label: "Bank A" }]);
  });
});

describe("review tools", () => {
  it("remembers a layout for the chosen institution and reports the re-read files", async () => {
    const detail = item("r-1", "new_institution", {
      alternatives: [
        {
          field: "institution_id",
          value: "i-1",
          label: "Bank A",
          source: "household",
        },
        {
          field: "institution_id",
          value: "i-2",
          label: "Bank B",
          source: "household",
        },
      ],
      regions: [{ page: 1, bbox: [0, 0, 612, 792], label: "page" }],
    });
    const api = toolsApi(detail, {
      "POST /api/v1/review/:id/remember-layout": () =>
        json({
          ambiguous: false,
          fingerprint_id: "f-1",
          institution_id: "i-2",
          item: { ...detail, state: "resolved" },
          kind: "statement",
          match: {},
          parser: "table_statement",
          reprocessed: ["d-2", "d-3"],
          version: 1,
        }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", {
      name: "Remember this layout",
    });
    expect(button).toBeDisabled();
    await user.selectOptions(
      await screen.findByLabelText("Institution"),
      "i-2",
    );
    await user.click(
      screen.getByLabelText(
        "Read files already received with this layout again",
      ),
    );
    await user.click(button);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/remember-layout")[0]?.body,
      ).toEqual({
        institution_id: "i-2",
        reprocess_similar: false,
        replace: false,
      });
    });
    expect(
      await screen.findByText(/2 file\(s\) are being read again/),
    ).toBeInTheDocument();
  });

  it("asks before replacing another taught layout of the institution", async () => {
    const detail = item("r-1", "new_institution", {
      alternatives: [
        {
          field: "institution_id",
          value: "i-1",
          label: "Bank A",
          source: "household",
        },
      ],
      regions: [{ page: 1, bbox: [0, 0, 612, 792], label: "page" }],
    });
    let calls = 0;
    const api = toolsApi(detail, {
      "POST /api/v1/review/:id/remember-layout": () => {
        calls += 1;
        return calls === 1
          ? problem(409, "layout-conflict")
          : json({
              ambiguous: false,
              fingerprint_id: "f-2",
              institution_id: "i-1",
              item: { ...detail, state: "resolved" },
              kind: "statement",
              match: {},
              parser: "statement_generic",
              reprocessed: ["d-1"],
              version: 2,
              kept: [],
            });
      },
    });
    renderApp("/review");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Institution"), "i-1");
    expect(screen.queryByLabelText(/Replace the layout remembered/)).toBeNull();
    const button = screen.getByRole("button", { name: "Remember this layout" });
    await user.click(button);
    const replace = await screen.findByLabelText(/Replace the layout remembered/);
    await user.click(replace);
    await user.click(button);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/remember-layout")[1]?.body,
      ).toEqual({ institution_id: "i-1", reprocess_similar: true, replace: true });
    });
  });

  it("highlights the doubtful row on the page and saves a chosen reading", async () => {
    const detail = item("r-2", "low_confidence_field", {
      reason: "low_confidence",
      payload: {
        reason: "low_confidence",
        transactions: [{ transaction_id: "t-1", fields: ["amount"] }],
      },
      regions: [
        {
          page: 1,
          bbox: [61.2, 79.2, 306, 396],
          label: "transaction",
          transaction_id: "t-1",
          fields: ["amount"],
        },
      ],
      alternatives: [
        {
          field: "amount",
          value: "24.10",
          source: "document",
          transaction_id: "t-1",
        },
      ],
    });
    const api = toolsApi(detail, {
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...detail, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const box = await screen.findByTestId("region-box");
    // An SVG rect in page units over the page (no style attribute, CSP).
    expect(box.tagName.toLowerCase()).toBe("rect");
    expect(box).not.toHaveAttribute("style");
    expect(box).toHaveAttribute("x", "61.2");
    expect(box).toHaveAttribute("y", "79.2");
    expect(box).toHaveAttribute("width", "244.8");
    expect(box).toHaveAttribute("height", "316.8");
    await user.click(
      await screen.findByRole("button", { name: "Use amount 24.10" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Save corrections and confirm" }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        confirm: true,
        import_as_read: false,
        transactions: [
          {
            transaction_id: "t-1",
            amount: { amount: "-24.10", currency: "USD" },
          },
        ],
      });
    });
  });

  it("corrects a held line and imports it", async () => {
    const detail = item("r-3", "low_confidence_field", {
      reason: "investment_lines_held",
      next_step: "correct_fields",
      payload: {
        reason: "investment_lines_held",
        account_id: "a-1",
        statement: 0,
        statement_key: "k".repeat(64),
        currency: "USD",
        fields: ["trade_date"],
        held: 2,
        imported: 5,
        lines: [
          {
            index: 4,
            fields: ["trade_date"],
            page: 2,
            date: "2025-06-05",
            type: "dividend",
            description: "EXAMPLE INDEX ETF",
            amount: "12.000000",
            section: "Income",
          },
          {
            index: 5,
            fields: ["security"],
            page: 2,
            date: "2026-06-09",
            type: "buy",
            description: "UNNAMED HOLDING",
            amount: "-30.000000",
            section: "Purchases",
          },
        ],
      },
    });
    const api = toolsApi(detail, {
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...detail, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Lines in doubt" });
    // Only a line whose doubt a person can settle by typing gets "Correct".
    const correct = within(table).getAllByRole("button", { name: "Correct" });
    expect(correct).toHaveLength(1);
    const send = screen.getByRole("button", {
      name: "Import the corrected lines",
    });
    expect(send).toBeDisabled();
    await user.click(correct[0] ?? table);
    const editor = screen.getByRole("group", { name: /^Correct the line of/ });
    expect(within(editor).getByLabelText("Amount")).toHaveValue("12.000000");
    expect(within(editor).getByLabelText("Kind")).toHaveValue("dividend");
    const day = within(editor).getByLabelText("Date");
    await user.clear(day);
    await user.type(day, "2026-06-05");
    // The other line cannot be corrected here. Importing would leave it out
    // for good, so nothing is sent until the person says so for that line.
    expect(send).toBeDisabled();
    expect(
      screen.getByText(/1 line is neither corrected nor left out/),
    ).toBeInTheDocument();
    const leave = within(table).getAllByRole("checkbox", { name: /^Leave out the line of/ });
    expect(leave).toHaveLength(2);
    expect(leave[1]).not.toBeChecked();
    await user.click(leave[1] ?? table);
    expect(send).toBeEnabled();
    await user.click(send);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        transactions: [],
        confirm: false,
        import_as_read: true,
        lines: [4],
        line_fixes: [{ line: 4, trade_date: "2026-06-05" }],
      });
    });
  });

  it("assigns a held drop-folder file to a member and remembers the subfolder", async () => {
    const detail = item("r-4", "unknown_owner");
    const api = toolsApi(detail, {
      "GET /api/v1/households/:id/members": () =>
        json([
          {
            user_id: "u-1",
            display_name: "Ana",
            role: "member",
            is_active: true,
            joined_at: NOW,
            username: "ana",
          },
        ]),
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...detail, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const assign = await screen.findByRole("button", {
      name: "Assign this file",
    });
    expect(assign).toBeDisabled();
    await user.selectOptions(
      await screen.findByLabelText("Whose file is it?"),
      "u-1",
    );
    await user.click(assign);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "unknown_owner",
        user_id: "u-1",
        remember: true,
      });
    });
  });

  it("shows the receipt page on request next to the suggestions", async () => {
    const detail = item("r-5", "receipt_match", {
      next_step: "attach_receipt",
      payload: {
        reason: "ambiguous",
        suggestions: [
          { transaction_id: "t-1", score: 0.9, amount: "exact", days: 0 },
        ],
      },
    });
    toolsApi(detail);
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      screen.queryByAltText("Page 1 of the document"),
    ).not.toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: "Show receipt" }),
    );
    expect(
      await screen.findByAltText("Page 1 of the document"),
    ).toHaveAttribute("src", "/api/v1/documents/d-1/preview?page=1");
  });
});
