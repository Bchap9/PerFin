// SPDX-License-Identifier: AGPL-3.0-or-later
/** One drawer per row: where it came from, how its category was decided, and "always" with a true preview. */
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, renderApp, type SessionOptions } from "../auth/testing";
import type { MerchantRulePreview } from "../rules/api";
import { CATEGORIES, category, lookupHandlers, NOW, page, tx } from "./fixtures";

const MERCHANT = { raw_payee: "SQ *QUIET BEAN COFFEE 0042", payee_id: null, merchant_key: "quiet bean coffee", merchant: "Quiet Bean Coffee" };
const ROW = tx("t-1", { ...MERCHANT, category_id: "c-2", category_source: "dictionary", category_confidence: "0.90", source_document_id: "d-7" });

function preview(extra: Partial<MerchantRulePreview> = {}): MerchantRulePreview {
  return {
    merchant_key: "quiet bean coffee",
    merchant: "Quiet Bean Coffee",
    teachable: true,
    reason: null,
    uncategorised: 17,
    guessed: 5,
    decided: 6,
    same_category: 2,
    existing_rule_id: null,
    shadowed_by: null,
    sample: [],
    ...extra,
  };
}

const rule = (id: string, name: string, categoryId: string) => ({
  id,
  name,
  enabled: true,
  stop_processing: false,
  conditions: { type: "merchant", key: "quiet bean coffee" },
  actions: [{ type: "set_category", category_id: categoryId }],
  hit_count: 0,
  last_hit_at: null,
  sort_order: 4,
  redacted: false,
  created_at: NOW,
  updated_at: NOW,
});

function setup(row = ROW, role: SessionOptions["role"] = "member", body: MerchantRulePreview = preview()) {
  // The server's copy of the row: a PATCH changes it, and later reads see the change.
  let current = row;
  const api = mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": () => json({ items: [current], next_cursor: null }),
    "GET /api/v1/transactions/:id": () => json(current),
    "POST /api/v1/rules/merchant/preview": () => json(body),
  });
  api.on("PATCH /api/v1/transactions/:id", (call) => {
    current = { ...current, ...(call.body as Partial<typeof row>), category_source: "person", category_confidence: "1.00", suggested_category_id: null };
    return json(current);
  });
  return api;
}

const drawer = (): Promise<HTMLElement> => screen.findByRole("dialog", { name: "Quiet Bean Coffee" });

describe("the transaction drawer", () => {
  it("opens from the address and shows where the row came from", async () => {
    setup();
    renderApp("/transactions?tx=t-1");
    const panel = within(await drawer());
    expect(panel.getByText("-$42.10")).toBeInTheDocument();
    expect(panel.getByText("Nov 1, 2026")).toBeInTheDocument();
    expect(await panel.findByRole("link", { name: "Everyday" })).toHaveAttribute("href", "/accounts/a-1");
    // The bank's own wording is always there.
    expect(panel.getByText("SQ *QUIET BEAN COFFEE 0042")).toBeInTheDocument();
    expect(panel.getByRole("link", { name: "The file it was imported from" })).toHaveAttribute("href", "/inbox/documents/d-7");
  });

  it("entered by hand when there is no document", async () => {
    setup({ ...ROW, source_document_id: null });
    renderApp("/transactions?tx=t-1");
    const panel = within(await drawer());
    expect(panel.getByText("Entered by hand")).toBeInTheDocument();
    expect(panel.queryByRole("link", { name: /imported from/ })).not.toBeInTheDocument();
  });

  it("shows how the category was decided", async () => {
    setup();
    renderApp("/transactions?tx=t-1");
    const panel = within(await drawer());
    expect(panel.getByText("Merchant list, 90%")).toBeInTheDocument();
  });

  it("a suggestion is a question with Accept", async () => {
    const api = setup({ ...ROW, category_id: null, suggested_category_id: "c-1", category_source: "dictionary", category_confidence: "0.60" });
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "accept_suggestions", changed: 1, results: [{ id: "t-1", result: "updated" }] }));
    renderApp("/transactions?tx=t-1");
    const panel = within(await drawer());
    expect(panel.getByText("Groceries?")).toBeInTheDocument();
    // The picker does not pretend the guess is the category.
    expect(panel.getByLabelText("Category")).toHaveValue("");
    await userEvent.setup().click(panel.getByRole("button", { name: "Accept Groceries" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "accept_suggestions", ids: ["t-1"] });
    });
  });

  it("choosing a category is the person's choice and can be undone", async () => {
    const api = setup();
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[0]?.body).toEqual({ category_id: "c-1" });
    });
    // At once, before the server answers: the person decided.
    expect(panel.getByText("You chose this")).toBeInTheDocument();
    const status = await panel.findByRole("status");
    expect(status).toHaveTextContent("Groceries is your choice for this transaction.");
    await user.click(panel.getByRole("button", { name: "Undo: put back Dining" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[1]?.body).toEqual({ category_id: "c-2" });
    });
  });

  it("the always box is unticked and asks the server for counts when ticked", async () => {
    const api = setup();
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    const box = panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" });
    expect(box).not.toBeChecked();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(0);
    await user.click(box);
    expect(await panel.findByText("17 transactions without a category")).toBeInTheDocument();
    expect(panel.getByText("5 that PerFin guessed differently will change")).toBeInTheDocument();
    expect(panel.getByText("6 that were chosen by hand or by another rule stay as they are")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")[0]?.body).toEqual({ transaction_id: "t-1", category_id: "c-2" });
    expect(panel.getByRole("checkbox", { name: "Also change the existing 22 transactions" })).not.toBeChecked();
  });

  it("existing rows change only when the second box is ticked", async () => {
    const job = { id: "j-1", type: "rules.run", state: "running", finished: false, attempts: 1, max_attempts: 3, created_at: NOW, updated_at: NOW };
    for (const ticked of [false, true]) {
      const api = setup();
      api.on("POST /api/v1/rules/merchant", () =>
        json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: false, previous_category_id: null, job_id: ticked ? "j-1" : null }, 201),
      );
      api.on("GET /api/v1/jobs/:id", () => json(job));
      api.on("POST /api/v1/rules/:id/revert", () => json({ rule_id: "r-9", reverted: 0, job_id: null }));
      renderApp("/transactions?tx=t-1");
      const user = userEvent.setup();
      const panel = within(await drawer());
      await user.click(panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" }));
      const second = await panel.findByRole("checkbox", { name: "Also change the existing 22 transactions" });
      if (ticked) {
        await user.click(second);
      }
      await user.click(panel.getByRole("button", { name: "Save rule" }));
      const saved = await panel.findByRole("group", { name: "Rule saved" });
      expect(api.callsTo("POST", "/api/v1/rules/merchant").map((call) => call.body)).toEqual([
        { transaction_id: "t-1", category_id: "c-2", apply_to_existing: ticked },
      ]);
      if (ticked) {
        expect(await within(saved).findByText("Running")).toBeInTheDocument();
      }
      // The undo of the new rule is in the drawer, where focus is.
      await user.click(within(saved).getByRole("button", { name: "Undo" }));
      await waitFor(() => {
        expect(api.callsTo("POST", "/api/v1/rules/r-9/revert")).toHaveLength(1);
      });
      cleanup();
    }
  });

  it("?always=t-1 opens with the box ticked and nothing saved", async () => {
    const api = setup({ ...ROW, category_id: "c-1", category_source: "learned", category_confidence: "0.95" });
    renderApp("/transactions?always=t-1");
    const panel = within(await drawer());
    expect(panel.getByRole("checkbox", { name: "Always use Groceries for Quiet Bean Coffee" })).toBeChecked();
    expect(await panel.findByText("17 transactions without a category")).toBeInTheDocument();
    expect(panel.getByRole("checkbox", { name: "Also change the existing 22 transactions" })).not.toBeChecked();
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(0);
  });

  it("a row that cannot teach says why", async () => {
    const api = setup(ROW, "member", preview({ shadowed_by: "r-3" }));
    api.on("GET /api/v1/rules", () => json({ items: [rule("r-3", "Coffee shops", "c-1")] }));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.click(panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" }));
    expect(await panel.findByRole("link", { name: "Coffee shops" })).toHaveAttribute("href", "/settings/rules/r-3");
    expect(panel.queryByRole("button", { name: "Save rule" })).not.toBeInTheDocument();
    expect(panel.queryByRole("checkbox", { name: /^Also change/ })).not.toBeInTheDocument();
    cleanup();

    // A row with no merchant says so without asking the server.
    const other = setup({ ...ROW, merchant_key: null, merchant: null, payee_id: "p-1" });
    renderApp("/transactions?tx=t-1");
    const plain = within(await screen.findByRole("dialog", { name: "Corner Market" }));
    expect(plain.getByText(/PerFin could not tell which merchant this is/)).toBeInTheDocument();
    expect(plain.queryByRole("checkbox", { name: /^Always use/ })).not.toBeInTheDocument();
    expect(other.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(0);
  });

  it("New category creates and selects", async () => {
    const api = setup();
    const created = category("c-9", "Coffee", { parent_id: null });
    api.on("POST /api/v1/categories", () => json(created, 201));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "New category…");
    const dialog = within(await screen.findByRole("dialog", { name: "New category" }));
    // Nothing is chosen by opening it.
    expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")).toHaveLength(0);
    api.on("GET /api/v1/categories", page([...CATEGORIES, created]));
    await user.type(dialog.getByLabelText(/Name/), "Coffee");
    await user.click(dialog.getByRole("button", { name: "Create category" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[0]?.body).toEqual({ category_id: "c-9" });
    });
    // It starts inside the group of the row's category.
    expect(api.callsTo("POST", "/api/v1/categories")[0]?.body).toEqual({ name: "Coffee", parent_id: "c-2" });
  });

  it("a viewer sees no picker, no always and no new category", async () => {
    const api = setup(ROW, "viewer");
    renderApp("/transactions?always=t-1");
    const panel = within(await drawer());
    // What the row is and why is there for everyone.
    expect(panel.getByText("Merchant list, 90%")).toBeInTheDocument();
    expect(panel.getByText("Dining")).toBeInTheDocument();
    expect(panel.queryByLabelText("Category")).not.toBeInTheDocument();
    expect(panel.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(panel.queryByText("New category…")).not.toBeInTheDocument();
    expect(panel.queryByRole("button", { name: "Edit details" })).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(0);
  });

  it("Escape closes, the address loses tx and focus returns to the row", async () => {
    setup();
    const { router } = renderApp("/transactions?category=c-2");
    const user = userEvent.setup();
    const open = await screen.findByRole("button", { name: "Open Quiet Bean Coffee, Nov 1, 2026, -$42.10" });
    await user.click(open);
    await drawer();
    expect(currentHref(router)).toBe("/transactions?category=c-2&tx=t-1");
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(currentHref(router)).toBe("/transactions?category=c-2");
    await waitFor(() => {
      expect(open).toHaveFocus();
    });
  });

  it("Back closes the drawer", async () => {
    setup();
    const { router } = renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Open Quiet Bean Coffee, Nov 1, 2026, -$42.10" }));
    await drawer();
    router.history.back();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(currentHref(router)).toBe("/transactions");
  });

  it("has no axe violations", async () => {
    setup();
    const { container } = renderApp("/transactions?tx=t-1");
    const panel = within(await drawer());
    await panel.findByRole("link", { name: "Everyday" });
    await userEvent.setup().click(panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" }));
    await panel.findByText("17 transactions without a category");
    await expectNoAxeViolations(container);
  });
});
