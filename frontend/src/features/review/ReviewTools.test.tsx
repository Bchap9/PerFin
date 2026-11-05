// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  authedSession,
  json,
  mockApi,
  renderApp,
  type Handler,
} from "../auth/testing";
import { account, lookupHandlers, NOW, tx } from "../transactions/fixtures";
import {
  fixFromAlternative,
  institutionChoices,
  layoutRequest,
  lowConfidenceResolution,
  mergeFix,
  ownerResolution,
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

function toolsApi(detail: ReviewItem, extra: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role: "owner" }),
    ...lookupHandlers([account("a-1", "Everyday")]),
    "GET /api/v1/review": () => json({ items: [detail], next_cursor: null }),
    "GET /api/v1/review/:id": () => json(detail),
    "GET /api/v1/documents/:id/regions": () => json(PAGE),
    "GET /api/v1/documents/:id": () =>
      json({
        id: "d-1",
        original_filename: "scan.pdf",
        mime_detected: "application/pdf",
      }),
    "GET /api/v1/transactions/:id": () => json(tx("t-1")),
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
      });
    });
    expect(
      await screen.findByText(/2 file\(s\) are being read again/),
    ).toBeInTheDocument();
  });

  it("highlights the doubtful row on the page and saves a chosen reading", async () => {
    const detail = item("r-2", "low_confidence_field", {
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
    expect(box).toHaveStyle({
      left: "10%",
      top: "10%",
      width: "40%",
      height: "40%",
    });
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
        transactions: [
          {
            transaction_id: "t-1",
            amount: { amount: "-24.10", currency: "USD" },
          },
        ],
      });
    });
  });

  it("drafts a rule from the item and saves it", async () => {
    const detail = item("r-3", "low_confidence_field", {
      regions: [
        {
          page: 1,
          bbox: [0, 0, 10, 10],
          label: "transaction",
          transaction_id: "t-1",
        },
      ],
    });
    const rule = {
      name: "Corner Market",
      enabled: true,
      stop_processing: false,
      conditions: { type: "payee", op: "contains", value: "CORNER" },
      actions: [],
    };
    const api = toolsApi(detail, {
      "GET /api/v1/review/:id/rule-draft": () =>
        json({ item_id: "r-3", transaction_id: "t-1", rule }),
      "POST /api/v1/rules": () => json({ id: "rule-9", ...rule }, 201),
    });
    renderApp("/review");
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "Create a rule from this" }),
    );
    expect(
      await screen.findByText("Draft rule: Corner Market"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save this rule" }));
    expect(
      await screen.findByRole("link", { name: /Rule saved/ }),
    ).toHaveAttribute("href", "/rules/rule-9");
    expect(api.callsTo("POST", "/api/v1/rules")[0]?.body).toEqual(rule);
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
