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
  type ReviewItem,
} from "./api";

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

function reviewApi(
  items: ReviewItem[],
  role: SessionOptions["role"] = "member",
  extra: Record<string, Handler> = {},
) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers([account("a-1", "Everyday"), account("a-2", "Savings")]),
    "GET /api/v1/review": () => json({ items, next_cursor: null }),
    "GET /api/v1/transactions/:id": (call) =>
      json(
        tx(call.path.split("/").at(-1) ?? "", {
          raw_payee: call.path.endsWith("new-1") ? "IMPORTED ROW" : "KEPT ROW",
        }),
      ),
    "GET /api/v1/documents/:id": () =>
      json({ id: "d-1", original_filename: "march.ofx" }),
    ...extra,
  });
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
    expect(await screen.findByText("Item resolved")).toBeInTheDocument();
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
      name: "Import into this account",
    });
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
      screen.queryByRole("button", { name: "Import into this account" }),
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
      within(dialog).getByRole("button", { name: "Import into this account" }),
    );
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")).toHaveLength(1);
    });
  });
});
