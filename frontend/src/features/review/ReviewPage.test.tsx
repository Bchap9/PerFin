// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, problem, renderApp, type Handler, type SessionOptions } from "../auth/testing";
import { account, lookupHandlers, NOW, tx } from "../transactions/fixtures";
import { accountResolution, duplicateResolution, reprocessResolution, type ReviewItem } from "./api";

function item(id: string, kind: ReviewItem["kind"], payload: Record<string, unknown>, extra: Partial<ReviewItem> = {}): ReviewItem {
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

function reviewApi(items: ReviewItem[], role: SessionOptions["role"] = "member", extra: Record<string, Handler> = {}) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers([account("a-1", "Everyday"), account("a-2", "Savings")]),
    "GET /api/v1/review": () => json({ items, next_cursor: null }),
    "GET /api/v1/transactions/:id": (call) => json(tx(call.path.split("/").at(-1) ?? "", { raw_payee: call.path.endsWith("new-1") ? "IMPORTED ROW" : "KEPT ROW" })),
    "GET /api/v1/documents/:id": () => json({ id: "d-1", original_filename: "march.ofx" }),
    ...extra,
  });
}

describe("resolution builders", () => {
  const near = item("r", "possible_duplicate", { transaction_id: "new-1", candidates: ["old-1", "old-2"] });
  it("needs the chosen candidate when several exist", () => {
    expect(duplicateResolution(near, "confirm", "old-2")).toEqual({ kind: "possible_duplicate", decision: "confirm", duplicate_of: "old-2" });
    expect(duplicateResolution(near, "confirm", null)).toEqual({ kind: "possible_duplicate", decision: "confirm" });
    expect(duplicateResolution(near, "reject", "old-2")).toEqual({ kind: "possible_duplicate", decision: "reject" });
    const single = item("r", "possible_duplicate", { transaction_id: "new-1", candidates: ["old-1"] });
    expect(duplicateResolution(single, "confirm", null).duplicate_of).toBe("old-1");
  });
  it("builds the other kinds", () => {
    expect(accountResolution("a-1", true)).toEqual({ kind: "unknown_account", account_id: "a-1", remember: true });
    expect(reprocessResolution()).toEqual({ kind: "unparsed", action: "reprocess" });
  });
});

describe("review queue", () => {
  it("shows a possible duplicate side by side and resolves it with the chosen row", async () => {
    const api = reviewApi([item("r-1", "possible_duplicate", { reason: "near_match", transaction_id: "new-1", candidates: ["old-1", "old-2"] })], "member", {
      "POST /api/v1/review/:id/resolve": () => json(item("r-1", "possible_duplicate", {}, { state: "resolved" })),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const group = await screen.findByRole("group", { name: "Compare transactions" });
    expect(await within(group).findByText("IMPORTED ROW")).toBeInTheDocument();
    expect((await within(group).findAllByText("KEPT ROW")).length).toBe(2);
    const confirm = screen.getByRole("button", { name: "It is a duplicate" });
    expect(confirm).toBeDisabled();
    await user.click(within(group).getByLabelText("Same as existing row 2"));
    await user.click(confirm);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body).toEqual({
        kind: "possible_duplicate",
        decision: "confirm",
        duplicate_of: "old-2",
      });
    });
    expect(await screen.findByText("Item resolved")).toBeInTheDocument();
  });

  it("rejects a duplicate flag with 'not a duplicate'", async () => {
    const api = reviewApi([item("r-1", "possible_duplicate", { reason: "same_content" })], "member", {
      "POST /api/v1/review/:id/resolve": () => json(item("r-1", "possible_duplicate", {}, { state: "resolved" })),
    });
    renderApp("/review");
    const user = userEvent.setup();
    expect(await screen.findByText(/march\.ofx/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Not a duplicate" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body).toEqual({ kind: "possible_duplicate", decision: "reject" });
    });
  });

  it("asks which account an unknown-account file belongs to", async () => {
    const api = reviewApi([item("r-2", "unknown_account", {})], "member", {
      "POST /api/v1/review/:id/resolve": () => json(item("r-2", "unknown_account", {}, { state: "resolved" })),
    });
    renderApp("/review");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Import into this account" });
    expect(button).toBeDisabled();
    await screen.findByRole("option", { name: "Savings" });
    await user.selectOptions(screen.getByLabelText("Account for this file"), "a-2");
    await user.click(screen.getByLabelText("Remember this account for the next file"));
    await user.click(button);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body).toEqual({ kind: "unknown_account", account_id: "a-2", remember: false });
    });
  });

  it("sends an unparsed file to the mapping editor, or processes it again", async () => {
    const api = reviewApi(
      [item("r-3", "unparsed", { reason: "needs_mapping" }, { next_step: "column_mapping" }), item("r-4", "unparsed", {}, { next_step: "reprocess" })],
      "member",
      { "POST /api/v1/review/:id/resolve": () => json(item("r-4", "unparsed", {}, { state: "resolved" })) },
    );
    renderApp("/review");
    const user = userEvent.setup();
    expect(await screen.findByRole("link", { name: "Map columns" })).toHaveAttribute("href", "/documents/d-1/mapping");
    const again = screen.getAllByRole("button", { name: "Process again" });
    await user.click(again[1] ?? again[0] ?? document.body);
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/resolve")[0]?.body).toEqual({ kind: "unparsed", action: "reprocess" });
    });
  });

  it("dismisses an item and shows a refusal from the server", async () => {
    const api = reviewApi([item("r-5", "new_institution", {})], "member", {
      "POST /api/v1/review/:id/dismiss": () => problem(409, "review-item-closed"),
    });
    renderApp("/review");
    const user = userEvent.setup();
    expect(await screen.findByText("This item can only be dismissed for now.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/review/:id/dismiss")).toHaveLength(1);
    });
    expect(await screen.findByText("detail of review-item-closed")).toBeInTheDocument();
  });

  it("is read-only for a viewer", async () => {
    reviewApi([item("r-6", "unknown_account", {}, { can_resolve: false })], "viewer");
    renderApp("/review");
    await screen.findByText("PerFin could not tell which account this file belongs to.");
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import into this account" })).not.toBeInTheDocument();
  });
});
