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
  type SessionOptions,
} from "../auth/testing";
import { account, lookupHandlers, NOW, tx } from "../transactions/fixtures";
import {
  accountResolution,
  duplicateResolution,
  payloadSuggestions,
  receiptResolution,
  reprocessResolution,
  type ReviewCard,
  type ReviewItem,
  type StatementReconciliation,
} from "./api";
import { explain } from "./reviewCopy";

/** A held statement's key (``statement_key``): its digest, in letters. */
const STATEMENT_KEY = "k".repeat(64);

function item(
  id: string,
  kind: ReviewItem["kind"],
  payload: Record<string, unknown>,
  extra: Partial<ReviewItem> = {},
): ReviewItem {
  return {
    id,
    kind,
    state: "open",
    document_id: "d-1",
    payload,
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: NOW,
    next_step: null,
    can_resolve: true,
    ...extra,
  };
}

const DOCUMENT: ReviewCard["document"] = {
  id: "d-1",
  original_filename: "october-export.csv",
  kind: "statement",
  status: "needs_review",
  mime_detected: "text/csv",
  source: "upload",
  created_at: NOW,
  period_start: null,
  period_end: null,
  previewable: false,
};

const HELD = new Set([
  "investment_reconciliation_failed",
  "investment_low_confidence",
  "investment_lines_held",
]);
const RECONCILE = new Set([
  "reconciliation_failed",
  "reconciliation_unavailable",
  "earlier_rows_kept",
]);
const ACKNOWLEDGE = new Set([
  "format_not_supported",
  "encrypted",
  "depth_limit",
  "container_incomplete",
]);

/** The action the server names for a card of this one item (its ``card_action``). */
function actionOf(one: ReviewItem): ReviewCard["action"] {
  const reason = one.reason ?? (one.payload["reason"] as string | undefined);
  switch (one.kind) {
    case "unknown_account":
      return "choose_account";
    case "unknown_owner":
      return "choose_owner";
    case "receipt_match":
      return "attach_receipt";
    case "possible_duplicate":
      return reason === "same_content_skipped"
        ? "acknowledge"
        : "confirm_duplicate";
    case "unparsed":
      if (one.payload["auto_import"] === "sign_unverified") {
        return "answer_sign";
      }
      if (one.next_step === "column_mapping") {
        return "map_columns";
      }
      return ACKNOWLEDGE.has(reason ?? "") ? "acknowledge" : "reprocess";
    case "low_confidence_field":
      if (RECONCILE.has(reason ?? "")) {
        return "reconcile";
      }
      return Array.isArray(one.payload["transactions"]) ||
        HELD.has(reason ?? "")
        ? "check_rows"
        : "acknowledge";
    default:
      return "acknowledge";
  }
}

/** One card: the document's open items and the one action (of the first item). */
function card(items: ReviewItem[], extra: Partial<ReviewCard> = {}): ReviewCard {
  const first = items[0];
  if (first === undefined) {
    throw new Error("a card needs an item");
  }
  return {
    document: first.document ?? DOCUMENT,
    items,
    action: actionOf(first),
    action_item_id: first.id,
    open: items.length,
    notices: 0,
    ...extra,
  };
}

/** The queue: each item wrapped in a card of its own, or the cards given. */
function reviewApi(
  items: ReviewItem[],
  role: SessionOptions["role"] = "member",
  extra: Record<string, Handler> = {},
  cards: ReviewCard[] = items.map((one) => card([one])),
) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers([account("a-1", "Everyday"), account("a-2", "Savings")]),
    "GET /api/v1/review/cards": () =>
      json({ cards, loose: [], next_cursor: null }),
    "GET /api/v1/transactions/:id": (call) =>
      json(
        tx(call.path.split("/").at(-1) ?? "", {
          raw_payee: call.path.includes("new-") ? "IMPORTED ROW" : "KEPT ROW",
        }),
      ),
    "GET /api/v1/documents/:id": () =>
      json({ id: "d-1", original_filename: "march.ofx" }),
    ...extra,
  });
}

/** Reads of one item's detail (the pattern also matches the cards list). */
function detailCalls(api: ReturnType<typeof mockApi>) {
  return api
    .callsTo("GET", "/api/v1/review/:id")
    .filter((call) => !call.path.endsWith("/cards"));
}

function cardOf(text: string | RegExp): HTMLElement {
  const found = screen.getByText(text).closest(".review-card");
  if (!(found instanceof HTMLElement)) {
    throw new Error("no card");
  }
  return found;
}

describe("resolution builders", () => {
  const near = item("r", "possible_duplicate", {
    transaction_id: "new-1",
    candidates: ["old-1", "old-2"],
  });
  it("needs the chosen candidate when several exist", () => {
    expect(duplicateResolution(near, "confirm", "old-2")).toEqual({
      kind: "possible_duplicate",
      decision: "confirm",
      duplicate_of: "old-2",
    });
    expect(duplicateResolution(near, "confirm", null)).toEqual({
      kind: "possible_duplicate",
      decision: "confirm",
    });
    expect(duplicateResolution(near, "reject", "old-2")).toEqual({
      kind: "possible_duplicate",
      decision: "reject",
    });
    const single = item("r", "possible_duplicate", {
      transaction_id: "new-1",
      candidates: ["old-1"],
    });
    expect(duplicateResolution(single, "confirm", null).duplicate_of).toBe(
      "old-1",
    );
  });
  it("builds the other kinds", () => {
    expect(accountResolution("a-1", true)).toEqual({
      kind: "unknown_account",
      account_id: "a-1",
      remember: true,
    });
    expect(reprocessResolution()).toEqual({
      kind: "unparsed",
      action: "reprocess",
    });
    expect(receiptResolution("t-1")).toEqual({
      kind: "receipt_match",
      transaction_id: "t-1",
    });
    expect(receiptResolution(null)).toEqual({
      kind: "receipt_match",
      transaction_id: null,
    });
  });
  it("reads receipt suggestions defensively", () => {
    const receipt = item("r", "receipt_match", {
      suggestions: [
        {
          transaction_id: "t-1",
          score: 0.8,
          amount: "exact",
          days: 1,
          already_linked: false,
        },
        { score: 1 },
        "junk",
      ],
    });
    expect(payloadSuggestions(receipt)).toEqual([
      {
        transactionId: "t-1",
        score: 0.8,
        amount: "exact",
        days: 1,
        alreadyLinked: false,
      },
    ]);
    expect(payloadSuggestions(item("r", "receipt_match", {}))).toEqual([]);
  });
});

describe("review queue", () => {
  it("shows a possible duplicate side by side and resolves it with the chosen row", async () => {
    const api = reviewApi(
      [
        item("r-1", "possible_duplicate", {
          reason: "near_match",
          transaction_id: "new-1",
          candidates: ["old-1", "old-2"],
        }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(item("r-1", "possible_duplicate", {}, { state: "resolved" })),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    const group = await screen.findByRole("group", {
      name: "Compare transactions",
    });
    expect(await within(group).findByText("IMPORTED ROW")).toBeInTheDocument();
    expect((await within(group).findAllByText("KEPT ROW")).length).toBe(2);
    const confirm = screen.getByRole("button", { name: "It is a duplicate" });
    expect(confirm).toBeDisabled();
    await user.click(within(group).getByLabelText("Same as existing row 2"));
    await user.click(confirm);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "possible_duplicate",
        decision: "confirm",
        duplicate_of: "old-2",
      });
    });
    // No toast: the card itself says what happened.
    expect(screen.queryByText("Item resolved")).not.toBeInTheDocument();
  });

  it("rejects a duplicate flag with 'not a duplicate'", async () => {
    const api = reviewApi(
      [item("r-1", "possible_duplicate", { reason: "same_content" })],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(item("r-1", "possible_duplicate", {}, { state: "resolved" })),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(await screen.findByText(/march\.ofx/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not a duplicate" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "possible_duplicate", decision: "reject" });
    });
  });

  it("lists the lines held back and imports them as read", async () => {
    const held = {
      reason: "investment_lines_held",
      account_id: "a-1",
      statement: 0,
      statement_key: STATEMENT_KEY,
      currency: "USD",
      fields: ["reconciliation"],
      failed: ["cash"],
      held: 2,
      imported: 9,
      differences: [],
      sections: [
        {
          label: "Total Checking Activity",
          printed: "-150.000000",
          computed: "-120.000000",
          difference: "30.000000",
          rows: 2,
          source: "total",
          page: 6,
        },
      ],
      lines: [
        {
          index: 4,
          fields: ["reconciliation"],
          page: 6,
          date: "2026-06-05",
          type: "withdrawal",
          description: "Check paid",
          amount: "-80.000000",
          section: "Checking Activity",
        },
        {
          index: 5,
          fields: ["reconciliation"],
          page: 6,
          date: "2026-06-12",
          type: "withdrawal",
          description: "Check paid",
          amount: "-40.000000",
          section: "Checking Activity",
        },
      ],
    };
    const api = reviewApi(
      [
        item("r-1", "low_confidence_field", held, {
          reason: "investment_lines_held",
          next_step: "correct_fields",
        }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(
            item("r-1", "low_confidence_field", held, { state: "resolved" }),
          ),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText(/the other 9 activity lines were imported/),
    ).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Lines in doubt" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getAllByText("Checking Activity")).toHaveLength(2);
    expect(within(table).getByText("-$80.00")).toBeInTheDocument();
    expect(
      within(table).getAllByText("its section does not add up"),
    ).toHaveLength(2);
    expect(within(table).getAllByText("Withdrawal")).toHaveLength(2);
    // The lines are not in the ledger: nothing to confirm row by row.
    expect(
      screen.queryByRole("button", { name: "Confirm as read" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import as read" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        transactions: [],
        confirm: false,
        import_as_read: true,
      });
    });
  });

  it("imports only the ticked lines when they stand alone", async () => {
    const held = {
      reason: "investment_lines_held",
      account_id: "a-1",
      statement: 0,
      statement_key: STATEMENT_KEY,
      currency: "USD",
      fields: ["reconciliation"],
      failed: ["cash"],
      held: 2,
      imported: 9,
      selectable: true,
      differences: [],
      sections: [],
      lines: [
        {
          index: 4,
          fields: ["reconciliation"],
          page: 6,
          date: "2026-06-05",
          type: "withdrawal",
          description: "Check paid",
          amount: "-80.000000",
          section: "Checking Activity",
        },
        {
          index: 5,
          fields: ["reconciliation"],
          page: 6,
          date: "2026-06-12",
          type: "withdrawal",
          description: "Check paid twice",
          amount: "-40.000000",
          section: "Checking Activity",
        },
      ],
    };
    const api = reviewApi(
      [
        item("r-1", "low_confidence_field", held, {
          reason: "investment_lines_held",
          next_step: "correct_fields",
        }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(
            item("r-1", "low_confidence_field", held, { state: "resolved" }),
          ),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "Lines in doubt" });
    const boxes = within(table).getAllByRole("checkbox");
    expect(boxes).toHaveLength(2);
    // Every line is ticked to begin with: the button imports them all.
    expect(boxes.every((box) => (box as HTMLInputElement).checked)).toBe(true);
    expect(
      screen.getByRole("button", { name: "Import as read" }),
    ).toBeInTheDocument();
    await user.click(
      within(table).getByRole("checkbox", { name: /Check paid twice/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "Import 1 of 2 lines" }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        transactions: [],
        confirm: false,
        import_as_read: true,
        lines: [4],
      });
    });
  });

  it("lists the holding that waits with its lines, without ticks", async () => {
    const held = {
      reason: "investment_lines_held",
      account_id: "a-1",
      statement: 0,
      statement_key: STATEMENT_KEY,
      currency: "USD",
      fields: ["market_value", "reconciliation"],
      failed: ["positions", "total_value"],
      held: 2,
      imported: 7,
      selectable: false,
      differences: [],
      sections: [],
      lines: [
        {
          index: 2,
          fields: ["held_with_security"],
          page: 4,
          date: "2026-06-05",
          type: "buy",
          description: "EXAMPLE INDEX ETF",
          amount: "-300.000000",
          section: "Securities Bought & Sold",
        },
        {
          index: 3,
          fields: ["held_with_security"],
          page: 4,
          date: "2026-06-19",
          type: "sell",
          description: "EXAMPLE INDEX ETF",
          amount: "150.000000",
          section: "Securities Bought & Sold",
        },
      ],
      positions: [
        {
          index: 1,
          symbol: "ZZETF",
          name: "EXAMPLE INDEX ETF",
          fields: ["market_value"],
          page: 3,
        },
      ],
    };
    reviewApi([
      item("r-1", "low_confidence_field", held, {
        reason: "investment_lines_held",
        next_step: "correct_fields",
      }),
    ]);
    renderApp("/review");
    const lines = await screen.findByRole("table", { name: "Lines in doubt" });
    // A security waits whole: its lines cannot be chosen one by one.
    expect(within(lines).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(lines).getAllByText("waits with its holding")).toHaveLength(2);
    const holdings = screen.getByRole("table", { name: "Holdings waiting" });
    expect(within(holdings).getByText("ZZETF")).toBeInTheDocument();
    expect(
      within(holdings).getByText("quantity × price is not the value printed"),
    ).toBeInTheDocument();
    // The value doubt is settled by the same answer when that check failed.
    expect(
      screen.getByRole("button", { name: "Import as read" }),
    ).toBeInTheDocument();
  });

  it("shows the kind the local model suggested and confirms it", async () => {
    const held = {
      reason: "investment_low_confidence",
      account_id: "a-1",
      statement: 0,
      statement_key: STATEMENT_KEY,
      fields: ["suggested_type"],
      reconciliation: "ok",
      positions: [],
      activity: [{ index: 2, fields: ["suggested_type"], page: 4 }],
      lines: [
        {
          index: 2,
          fields: ["suggested_type"],
          page: 4,
          date: "2026-06-05",
          type: "buy",
          description: "EXAMPLE INDEX ETF",
          amount: "-300.000000",
          section: "Securities Bought & Sold",
        },
      ],
    };
    const api = reviewApi(
      [
        item("r-1", "low_confidence_field", held, {
          reason: "investment_low_confidence",
          next_step: "correct_fields",
        }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(
            item("r-1", "low_confidence_field", held, { state: "resolved" }),
          ),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText("Check what the local model suggested"),
    ).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Lines in doubt" });
    expect(within(table).getByText("Purchase")).toBeInTheDocument();
    expect(
      within(table).getByText("reading suggested by the local model"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/confirms the kinds the local model suggested/),
    ).toBeInTheDocument();
    // Not a date problem: no column mapping is offered for it.
    expect(
      screen.queryByRole("link", { name: "Map columns" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import as read" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        transactions: [],
        confirm: false,
        import_as_read: true,
      });
    });
  });

  it("imports a brokerage statement that does not add up as read", async () => {
    const held = {
      reason: "investment_reconciliation_failed",
      account_id: "a-1",
      statement: 1,
      statement_key: STATEMENT_KEY,
      currency: "USD",
      failed: ["cash"],
      differences: [
        {
          check: "cash",
          printed: "412.150000",
          computed: "412.450000",
          difference: "0.300000",
        },
      ],
    };
    const api = reviewApi(
      [
        item("r-1", "low_confidence_field", held, {
          reason: "investment_reconciliation_failed",
          next_step: "correct_fields",
        }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(
            item("r-1", "low_confidence_field", held, { state: "resolved" }),
          ),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText(/a difference of \$0\.30/),
    ).toBeInTheDocument();
    expect(screen.getByText(/closing cash of \$412\.15/)).toBeInTheDocument();
    expect(
      screen.getByText(/because its totals do not add up\./),
    ).toBeInTheDocument();
    // Nothing of it is in the ledger: no rows to confirm.
    expect(
      screen.queryByRole("button", { name: "Confirm as read" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Dismiss" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import as read" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "low_confidence_field",
        transactions: [],
        confirm: false,
        import_as_read: true,
      });
    });
  });

  it("offers no 'import as read' for a statement whose dates are a guess", async () => {
    reviewApi([
      item(
        "r-1",
        "low_confidence_field",
        {
          reason: "investment_low_confidence",
          account_id: "a-1",
          statement_key: STATEMENT_KEY,
          fields: ["reconciliation", "trade_date"],
        },
        { reason: "investment_low_confidence" },
      ),
    ]);
    renderApp("/review");
    expect(await screen.findByText("Dates may be wrong")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Import as read" }),
    ).not.toBeInTheDocument();
  });

  it("offers 'import as read' for a statement with nothing to check against", async () => {
    reviewApi([
      item(
        "r-1",
        "low_confidence_field",
        {
          reason: "investment_low_confidence",
          account_id: "a-1",
          statement_key: STATEMENT_KEY,
          fields: ["reconciliation"],
        },
        { reason: "investment_low_confidence" },
      ),
    ]);
    renderApp("/review");
    expect(
      await screen.findByText("Nothing to check against"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Import as read" }),
    ).toBeInTheDocument();
    // Nothing failed to add up: its totals could not be checked.
    expect(
      screen.getByText(/because its totals could not be checked\./),
    ).toBeInTheDocument();
    expect(screen.queryByText(/do not add up/)).not.toBeInTheDocument();
  });

  it("offers no 'import as read' on an item that names no statement", async () => {
    // Raised before items named their statement: reprocessing raises a current one.
    reviewApi([
      item(
        "r-1",
        "low_confidence_field",
        {
          reason: "investment_reconciliation_failed",
          account_id: "a-1",
          failed: ["cash"],
        },
        { reason: "investment_reconciliation_failed" },
      ),
    ]);
    renderApp("/review");
    expect(
      await screen.findByText("Statement does not add up"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Import as read" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/“Import as read”/)).not.toBeInTheDocument();
  });

  it("offers 'same statement' and 'import anyway' for an overlapping statement", async () => {
    const overlap = {
      reason: "statement_overlap",
      account_id: "a-1",
      statement: 0,
      period_start: "2026-03-01",
      period_end: "2026-03-31",
      other_document_id: "d-1",
      other_period_start: "2026-03-01",
      other_period_end: "2026-03-31",
    };
    const api = reviewApi(
      [item("r-1", "possible_duplicate", overlap)],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(
            item("r-1", "possible_duplicate", overlap, { state: "resolved" }),
          ),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    const group = await screen.findByRole("group", {
      name: "Compare statements",
    });
    expect(
      await within(group).findByRole("link", { name: "march.ofx" }),
    ).toHaveAttribute("href", "/documents/d-1");
    expect(
      screen.getByRole("button", { name: "Import anyway" }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Same statement — skip" }),
    );
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "possible_duplicate", decision: "confirm" });
    });
  });

  it("asks which account an unknown-account file belongs to", async () => {
    const api = reviewApi([item("r-2", "unknown_account", {})], "member", {
      "POST /api/v1/review/:id/resolve": () =>
        json(item("r-2", "unknown_account", {}, { state: "resolved" })),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", {
      name: "Use this account",
    });
    // No ranking came with the item: nothing is chosen for the person.
    expect(button).toBeDisabled();
    await screen.findByRole("option", { name: "Savings" });
    await user.selectOptions(
      screen.getByLabelText("Account for this file"),
      "a-2",
    );
    await user.click(
      screen.getByLabelText("Remember this account for the next file"),
    );
    await user.click(button);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "unknown_account",
        account_id: "a-2",
        remember: false,
      });
    });
  });

  it("sends an unparsed file to the mapping editor, or processes it again", async () => {
    const api = reviewApi(
      [
        item(
          "r-3",
          "unparsed",
          { reason: "needs_mapping" },
          { next_step: "column_mapping" },
        ),
        item("r-4", "unparsed", {}, { next_step: "reprocess" }),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(item("r-4", "unparsed", {}, { state: "resolved" })),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByRole("link", { name: "Map columns" }),
    ).toHaveAttribute("href", "/documents/d-1/mapping");
    const again = screen.getAllByRole("button", { name: "Process again" });
    await user.click(again[1] ?? again[0] ?? document.body);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "unparsed", action: "reprocess" });
    });
  });

  it("attaches a receipt to the suggestion chosen, or to no transaction", async () => {
    const api = reviewApi(
      [
        item(
          "r-7",
          "receipt_match",
          {
            reason: "ambiguous",
            suggestions: [
              {
                transaction_id: "old-1",
                score: 0.95,
                amount: "exact",
                days: 0,
                already_linked: false,
              },
              {
                transaction_id: "old-2",
                score: 0.9,
                amount: "tip",
                days: 1,
                already_linked: true,
              },
            ],
          },
          { next_step: "attach_receipt" },
        ),
      ],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json(item("r-7", "receipt_match", {}, { state: "resolved" })),
      },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText(
        "Several transactions fit this receipt equally well.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "amount plus a tip, 1 day(s) apart, already has a receipt",
      ),
    ).toBeInTheDocument();
    const attach = screen.getByRole("button", { name: "Attach receipt" });
    expect(attach).toBeDisabled();
    await user.click(screen.getByLabelText("Attach to suggestion 2"));
    await user.click(attach);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "receipt_match", transaction_id: "old-2" });
    });
    await user.click(screen.getByRole("button", { name: "No transaction" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[1]?.body,
      ).toEqual({ kind: "receipt_match", transaction_id: null });
    });
  });

  it("dismisses an item and shows a refusal from the server", async () => {
    const api = reviewApi([item("r-5", "rejected_email", {})], "member", {
      "POST /api/v1/review/:id/dismiss": () =>
        problem(409, "review-item-closed"),
    });
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText("This email was not accepted."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/dismiss")).toHaveLength(1);
    });
    expect(
      await screen.findByText("detail of review-item-closed"),
    ).toBeInTheDocument();
  });

  it("says which part of a multi-account file needs an account", async () => {
    reviewApi([
      item("r-7", "unknown_account", {
        reason: "account_not_found",
        group: 1,
        transactions: 12,
      }),
    ]);
    renderApp("/review");
    expect(
      await screen.findByText(
        "This file lists several accounts; 12 of its rows belong to an account PerFin does not know.",
      ),
    ).toBeInTheDocument();
  });

  it("is read-only for a viewer", async () => {
    reviewApi(
      [item("r-6", "unknown_account", {}, { can_resolve: false })],
      "viewer",
    );
    renderApp("/review");
    await screen.findByText(
      "PerFin could not tell which account this file belongs to.",
    );
    expect(
      screen.queryByRole("button", { name: "Dismiss" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Use this account" }),
    ).not.toBeInTheDocument();
  });

  it("names the file an item is about and explains how to fix it", async () => {
    const api = reviewApi([
      item(
        "r-8",
        "unknown_account",
        { reason: "account_not_found", statement: 0 },
        {
          reason: "account_not_found",
          document: {
            id: "d-1",
            original_filename: "october-statement.pdf",
            kind: "statement",
            status: "needs_review",
            mime_detected: "application/pdf",
            source: "upload",
            created_at: NOW,
            period_start: "2026-10-01",
            period_end: "2026-10-31",
            previewable: true,
          },
        },
      ),
    ]);
    renderApp("/review");
    const user = userEvent.setup();
    const link = await screen.findByRole("link", {
      name: "october-statement.pdf",
    });
    expect(link).toHaveAttribute("href", "/documents/d-1");
    expect(
      screen.getByText(
        "None of your accounts matches the account number in this file.",
      ),
    ).toBeInTheDocument();
    // The steps are in the Fix dialog, with the document and the controls.
    expect(screen.queryByText("How to fix it")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fix\u2026" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("How to fix it")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        /PerFin matches files to accounts by the last four digits/,
      ),
    ).toBeInTheDocument();
    await user.selectOptions(
      within(dialog).getByLabelText("Account for this file"),
      "a-1",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "Use this account" }),
    );
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")).toHaveLength(1);
    });
  });
});

const FAILED = item(
  "r-10",
  "low_confidence_field",
  {
    reason: "reconciliation_failed",
    account_id: "a-1",
    statement: 0,
    opening: "500.00",
    closing: "434.50",
    difference: "-12.00",
  },
  { reason: "reconciliation_failed", next_step: "correct_fields" },
);
const SKIPPED = item(
  "r-11",
  "low_confidence_field",
  { reason: "lines_skipped", count: 2 },
  { reason: "lines_skipped", next_step: "correct_fields" },
);
const DOUBTED = item(
  "r-12",
  "low_confidence_field",
  {
    reason: "low_confidence",
    account_id: "a-1",
    count: 1,
    transactions: [{ transaction_id: "t-9", fields: ["amount"] }],
  },
  { reason: "low_confidence", next_step: "correct_fields" },
);

function threeFindings(): ReviewCard {
  return card([FAILED, SKIPPED, DOUBTED], { action: "reconcile" });
}

const STATEMENT: StatementReconciliation = {
  statement: 0,
  account_id: "a-1",
  account_name: "Everyday",
  can_edit: true,
  causes: [],
  opening: "500.00",
  closing: "434.50",
  previous_closing: null,
  currency: "USD",
  total: "-77.50",
  difference: "-12.00",
  lines_skipped: 0,
  period_start: "2026-10-01",
  period_end: "2026-10-31",
  printed_sign: "ledger",
  removed: [],
  rows: [],
  rows_truncated: false,
  status: "off",
  verification: "unverified",
};

function accountItem(scores: readonly [number, number]): ReviewItem {
  return item(
    "r-20",
    "unknown_account",
    { reason: "account_not_found", statement: 0 },
    {
      reason: "account_not_found",
      next_step: "choose_account",
      alternatives: [
        {
          field: "account_id",
          value: "a-2",
          label: "Savings",
          score: scores[0],
          source: "ledger",
        },
        {
          field: "account_id",
          value: "a-1",
          label: "Everyday",
          score: scores[1],
          source: "ledger",
        },
      ],
    },
  );
}

describe("review cards", () => {
  it("shows one card for a document with three findings", async () => {
    reviewApi([], "member", {}, [threeFindings()]);
    const { container } = renderApp("/review");
    expect(
      await screen.findByRole("heading", { name: "3 thing(s) to settle" }),
    ).toBeInTheDocument();
    expect(container.querySelectorAll(".review-card")).toHaveLength(1);
    const found = cardOf("october-export.csv");
    for (const one of [FAILED, SKIPPED, DOUBTED]) {
      expect(
        within(found).getByRole("heading", { name: explain(one).title }),
      ).toBeInTheDocument();
    }
    expect(within(found).getAllByRole("button", { name: "Dismiss" })).toHaveLength(3);
  });

  it("the card's only primary button is its action", async () => {
    reviewApi([], "member", {}, [threeFindings()]);
    renderApp("/review");
    await screen.findByRole("heading", { name: "3 thing(s) to settle" });
    const primary = cardOf("october-export.csv").querySelectorAll(".btn--primary");
    expect(primary).toHaveLength(1);
    expect(primary[0]).toHaveTextContent("Check against the statement");
  });

  it("says a card of notices is for information", async () => {
    const notice = item(
      "r-13",
      "low_confidence_field",
      { reason: "investment_figures_differ" },
      { reason: "investment_figures_differ" },
    );
    reviewApi([], "member", {}, [
      card([notice], { action: "acknowledge", open: 0, notices: 1 }),
    ]);
    renderApp("/review");
    expect(
      await screen.findByRole("heading", { name: "For your information" }),
    ).toBeInTheDocument();
  });

  it("pre-selects the account that ranks first", async () => {
    const ranked = accountItem([0.85, 0.15]);
    const api = reviewApi([ranked], "member", {
      "GET /api/v1/review/:id": () => json(ranked),
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...ranked, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Account for this file");
    await waitFor(() => {
      expect(select).toHaveValue("a-2");
    });
    await user.click(screen.getByRole("button", { name: "Use this account" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "unknown_account", account_id: "a-2", remember: true });
    });
  });

  it("does not pre-select on a tie", async () => {
    const tied = accountItem([0.15, 0.15]);
    const api = reviewApi([tied], "member", {
      "GET /api/v1/review/:id": () => json(tied),
    });
    renderApp("/review");
    const select = await screen.findByLabelText("Account for this file");
    await waitFor(() => {
      expect(detailCalls(api)).toHaveLength(1);
    });
    await screen.findByRole("option", { name: "Savings" });
    expect(select).toHaveValue("");
    expect(screen.getByRole("button", { name: "Use this account" })).toBeDisabled();
  });

  it("answers the sign question in one click", async () => {
    const sign = item(
      "r-30",
      "unparsed",
      {
        reason: "mapping_required",
        auto_import: "sign_unverified",
        scope: "mapping",
        account_id: "a-1",
        group: 0,
        transactions: 14,
      },
      { reason: "mapping_required", next_step: "column_mapping" },
    );
    const api = reviewApi([sign], "member", {
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...sign, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText("Positive amounts in this file are:"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "PerFin remembers the answer for files with the same columns.",
      ),
    ).toBeInTheDocument();
    // The question is answered here: nobody is sent to the mapping editor.
    expect(
      screen.queryByRole("link", { name: "Map columns" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Money in" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Money out" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "unparsed",
        action: "answer_sign",
        positive_is: "money_out",
      });
    });
  });

  it("says a sign answer for one account stays with this file", async () => {
    reviewApi([
      item(
        "r-31",
        "unparsed",
        {
          reason: "mapping_required",
          auto_import: "sign_unverified",
          scope: "account",
          account_id: "a-1",
        },
        { reason: "mapping_required", next_step: "column_mapping" },
      ),
    ]);
    renderApp("/review");
    expect(
      await screen.findByText(
        "The answer is kept for this account in this file.",
      ),
    ).toBeInTheDocument();
  });

  it("decides several near duplicates at once", async () => {
    const near = item(
      "r-40",
      "possible_duplicate",
      {
        reason: "near_matches",
        account_id: "a-1",
        statement: 0,
        count: 3,
        rows: [
          { transaction_id: "new-1", candidates: ["old-1"] },
          { transaction_id: "new-2", candidates: ["old-2"] },
          { transaction_id: "new-3", candidates: ["old-3"] },
        ],
      },
      { reason: "near_matches", next_step: "confirm_duplicate" },
    );
    const api = reviewApi([near], "member", {
      "POST /api/v1/review/:id/resolve": () =>
        json({ ...near, state: "resolved" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    expect(
      await screen.findByText(
        "3 rows of this statement have the same date and amount as rows already in the account.",
      ),
    ).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save these answers" });
    expect(save).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "All are the same rows" }),
    );
    await user.click(
      within(screen.getByRole("group", { name: "Row 3" })).getByLabelText(
        "Different row",
      ),
    );
    await user.click(save);
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({
        kind: "possible_duplicate",
        decision: "confirm",
        rows: [
          { transaction_id: "new-1", decision: "confirm", duplicate_of: "old-1" },
          { transaction_id: "new-2", decision: "confirm", duplicate_of: "old-2" },
          { transaction_id: "new-3", decision: "reject" },
        ],
      });
    });
  });

  it("offers no rule button and no confirm on a card with nothing pending", async () => {
    // The rows the item listed are gone: its detail names none.
    const api = reviewApi([DOUBTED], "member", {
      "GET /api/v1/review/:id": () =>
        json({ ...DOUBTED, regions: [], alternatives: [] }),
    });
    renderApp("/review");
    await screen.findByRole("heading", { name: "1 thing(s) to settle" });
    await waitFor(() => {
      expect(detailCalls(api)).toHaveLength(1);
    });
    expect(await screen.findByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Confirm as read" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Create a rule from this" }),
    ).not.toBeInTheDocument();
  });

  it("shows the row tools only for the item that lists rows", async () => {
    const api = reviewApi([SKIPPED]);
    renderApp("/review");
    await screen.findByRole("heading", { name: "1 thing(s) to settle" });
    expect(
      screen.queryByRole("button", { name: "Confirm as read" }),
    ).not.toBeInTheDocument();
    expect(detailCalls(api)).toHaveLength(0);
  });

  it("an encrypted file can only be acknowledged or dismissed", async () => {
    const locked = item(
      "r-50",
      "unparsed",
      { reason: "encrypted" },
      { reason: "encrypted", next_step: "reprocess" },
    );
    const api = reviewApi([locked], "member", {
      "POST /api/v1/review/:id/dismiss": () =>
        json({ ...locked, state: "dismissed" }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const ok = await screen.findByRole("button", { name: "OK" });
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    for (const name of ["Process again", "Teach PerFin", "Confirm as read"]) {
      expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
    }
    expect(
      screen.queryByRole("link", { name: "Map columns" }),
    ).not.toBeInTheDocument();
    await user.click(ok);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/dismiss")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/review/:id/resolve")).toHaveLength(0);
  });

  it("acknowledges a skipped copy, or imports it anyway", async () => {
    const copy = item(
      "r-51",
      "possible_duplicate",
      { reason: "same_content_skipped", duplicate_of: "d-0" },
      { reason: "same_content_skipped", next_step: "confirm_duplicate" },
    );
    const api = reviewApi(
      [],
      "member",
      {
        "POST /api/v1/review/:id/resolve": () =>
          json({ ...copy, state: "resolved" }),
      },
      [card([copy], { open: 0, notices: 1 })],
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(await screen.findByText("Already uploaded")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OK" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Import it anyway" }));
    await waitFor(() => {
      expect(
        api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body,
      ).toEqual({ kind: "possible_duplicate", decision: "reject" });
    });
  });

  it("opens the reconcile view from the card", async () => {
    const api = reviewApi(
      [],
      "member",
      {
        "GET /api/v1/documents/:id/reconciliation": () =>
          json({ document_id: "d-1", statements: [STATEMENT] }),
      },
      [threeFindings()],
    );
    renderApp("/review");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Check against the statement" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      await within(dialog).findByRole("heading", {
        name: "Check against the statement",
      }),
    ).toBeInTheDocument();
    expect(
      api.callsTo("GET", "/api/v1/documents/:id/reconciliation")[0]?.path,
    ).toBe("/api/v1/documents/d-1/reconciliation");
    await user.click(within(dialog).getByRole("button", { name: "Finish later" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("after the last answer the card says where the rows went", async () => {
    const ranked = accountItem([0.85, 0.15]);
    const api = reviewApi([ranked], "member", {
      "GET /api/v1/review/:id": () => json(ranked),
      "POST /api/v1/review/:id/resolve": () => {
        api.on("GET /api/v1/review/cards", () =>
          json({ cards: [], loose: [], next_cursor: null }),
        );
        return json({ ...ranked, state: "resolved" });
      },
      "GET /api/v1/documents/:id/imported": () =>
        json({
          transactions: 12,
          investment_transactions: 0,
          loan_payments: 0,
          balance_snapshots: 0,
          child_documents: 0,
          skipped_lines: 0,
          statements: [
            {
              account_id: "a-2",
              account_name: "Savings",
              kind: "bank",
              outcome: "imported",
              pending: 0,
              period_start: null,
              period_end: null,
              source_format: "csv",
              transactions: 12,
              unchecked: false,
              verification: "unverified",
            },
          ],
        }),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Account for this file");
    await waitFor(() => {
      expect(select).toHaveValue("a-2");
    });
    await user.click(screen.getByRole("button", { name: "Use this account" }));
    expect(
      await screen.findByText("Settled. 12 row(s) are in Savings."),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View the rows" })).toHaveAttribute(
      "href",
      "/transactions?document=d-1",
    );
    expect(screen.getByRole("link", { name: "Open the file" })).toHaveAttribute(
      "href",
      "/documents/d-1",
    );
    expect(screen.queryByText("Item resolved")).not.toBeInTheDocument();
    expect(screen.queryByText("Nothing to review")).not.toBeInTheDocument();
  });

  it("filters to one document from the query string", async () => {
    const other: ReviewCard["document"] = {
      ...DOCUMENT,
      id: "d-2",
      original_filename: "november-export.csv",
    };
    reviewApi([], "member", {}, [
      card([item("r-60", "unparsed", {}, { next_step: "reprocess" })]),
      card(
        [
          item(
            "r-61",
            "unparsed",
            {},
            { next_step: "reprocess", document_id: "d-2", document: other },
          ),
        ],
      ),
    ]);
    renderApp("/review?document=d-2");
    expect(
      await screen.findByRole("link", { name: "november-export.csv" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "october-export.csv" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Show every file" }),
    ).toHaveAttribute("href", "/review");
  });

  it("lists resolved items of one document from the items endpoint", async () => {
    const done = item(
      "r-70",
      "unparsed",
      {},
      { state: "resolved", document_id: "d-2" },
    );
    const api = reviewApi([], "member", {
      "GET /api/v1/review": () => json({ items: [done], next_cursor: null }),
    });
    renderApp("/review?document=d-2");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Show"), "resolved");
    expect(
      await screen.findByRole("heading", { name: "Could not be read" }),
    ).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/review")[0]?.search).toContain(
      "document=d-2",
    );
    expect(api.callsTo("GET", "/api/v1/review")[0]?.search).toContain(
      "state=resolved",
    );
  });
});
