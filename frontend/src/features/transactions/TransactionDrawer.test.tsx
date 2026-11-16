// SPDX-License-Identifier: AGPL-3.0-or-later
/** One drawer per row: where it came from, how its category was decided, and "always" with a true preview. */
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, currentHref, json, mockApi, noContent, renderApp, type SessionOptions } from "../auth/testing";
import type { MerchantRulePreview } from "../rules/api";
import { CATEGORIES, category, lookupHandlers, NOW, page, tx } from "./fixtures";
import { decidedBy } from "./TransactionDrawer";

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

  it("a chosen category shows at once as the person's, and a refusal puts the guess back", async () => {
    const api = setup();
    let answer: (response: Response) => void = () => undefined;
    api.on("PATCH /api/v1/transactions/:id", () => new Promise<Response>((resolve) => (answer = resolve)));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[0]?.body).toEqual({ category_id: "c-1" });
    });
    // At once, before the server answers: the person decided.
    expect(panel.getByText("You chose this")).toBeInTheDocument();
    expect(panel.getByLabelText("Category")).toHaveValue("c-1");
    // A fresh read of the row never answers, so only the drawer's own rollback can put the guess back.
    api.on("GET /api/v1/transactions/:id", () => new Promise<Response>(() => undefined));
    answer(json({ type: "urn:perfin:problem:conflict", title: "Conflict", status: 409, detail: "The row changed meanwhile." }, 409));
    expect(await panel.findByRole("alert")).toHaveTextContent("The row changed meanwhile.");
    expect(panel.getByLabelText("Category")).toHaveValue("c-2");
    expect(panel.getByText("Merchant list, 90%")).toBeInTheDocument();
    expect(panel.queryByText("You chose this")).not.toBeInTheDocument();
  });

  it("choosing a category is the person's choice, and their own earlier choice can be undone", async () => {
    const api = setup({ ...ROW, category_source: "person", category_confidence: "1.00" });
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[0]?.body).toEqual({ category_id: "c-1" });
    });
    expect(panel.getByText("You chose this")).toBeInTheDocument();
    const status = await panel.findByRole("status");
    expect(status).toHaveTextContent("Groceries is your choice for this transaction.");
    await user.click(panel.getByRole("button", { name: "Undo: put back Dining" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[1]?.body).toEqual({ category_id: "c-2" });
    });
    expect(await panel.findByText("Dining is back, as your choice.")).toBeInTheDocument();
  });

  it("a tag changed while the category is still saving does not lose the category's notice", async () => {
    const api = setup();
    // The category's PATCH waits until the tag has been clicked.
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let current: typeof ROW = ROW;
    api.on("PATCH /api/v1/transactions/:id", async (call) => {
      const patch = call.body as Partial<typeof ROW>;
      if ("category_id" in patch) {
        await held;
      }
      current = { ...current, ...patch, category_source: "person", category_confidence: "1.00", suggested_category_id: null };
      return json(current);
    });
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")).toHaveLength(1);
    });
    const tags = within(await panel.findByRole("group", { name: "Tags" }));
    await user.click(tags.getByRole("checkbox", { name: "trip" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")).toHaveLength(2);
    });
    release();
    expect(await panel.findByRole("status")).toHaveTextContent("Groceries is your choice for this transaction.");
  });

  it("after a guess, going back says it becomes the person's choice", async () => {
    const api = setup();
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    expect(await panel.findByRole("status")).toHaveTextContent("Groceries is your choice for this transaction. Before, it was Dining (Merchant list, 90%).");
    // The server cannot make Dining a guess again: nothing calls it an undo.
    expect(panel.queryByRole("button", { name: /^Undo/ })).not.toBeInTheDocument();
    await user.click(panel.getByRole("button", { name: "Choose Dining again, as your choice" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[1]?.body).toEqual({ category_id: "c-2" });
    });
    expect(await panel.findByText("Dining is back, as your choice.")).toBeInTheDocument();
  });

  it("after a suggestion, nothing offers to clear the category as if that were an undo", async () => {
    const api = setup({ ...ROW, category_id: null, suggested_category_id: "c-1", category_source: "dictionary", category_confidence: "0.60" });
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-2");
    expect(await panel.findByRole("status")).toHaveTextContent(
      "Dining is your choice for this transaction. Before, it had no category and PerFin suggested Groceries; a suggestion cannot be put back.",
    );
    expect(panel.queryByRole("button", { name: /^Undo|again/ })).not.toBeInTheDocument();
    expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")).toHaveLength(1);
  });

  it("Edit details, Split, Mark as transfer and the note each open their dialog", async () => {
    const opens: [string, string | null, string | RegExp][] = [
      ["Edit details", null, "Edit transaction"],
      ["Split", null, "Split transaction"],
      ["More actions for SQ *QUIET BEAN COFFEE 0042", "Mark as transfer", "Link as transfer"],
      ["More actions for SQ *QUIET BEAN COFFEE 0042", "Add note…", "Note on SQ *QUIET BEAN COFFEE 0042"],
    ];
    for (const [button, item, title] of opens) {
      setup();
      renderApp("/transactions?tx=t-1");
      const user = userEvent.setup();
      const panel = within(await drawer());
      await user.click(panel.getByRole("button", { name: button }));
      if (item !== null) {
        await user.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: item }));
      }
      expect(await screen.findByRole("dialog", { name: title })).toBeInTheDocument();
      cleanup();
    }
  });

  it("Delete asks, deletes, closes the drawer and leaves the row out of the selection", async () => {
    const api = setup();
    api.on("DELETE /api/v1/transactions/:id", () => noContent());
    const { router } = renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "Select SQ *QUIET BEAN COFFEE 0042" }));
    expect(screen.getByRole("group", { name: "Actions for 1 selected" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open Quiet Bean Coffee, Nov 1, 2026, -$42.10" }));
    const panel = within(await drawer());
    await user.click(panel.getByRole("button", { name: "More actions for SQ *QUIET BEAN COFFEE 0042" }));
    await user.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Delete" }));
    const confirm = within(await screen.findByRole("dialog", { name: "Delete this transaction?" }));
    expect(api.callsTo("DELETE", "/api/v1/transactions/t-1")).toHaveLength(0);
    await user.click(confirm.getByRole("button", { name: "Delete transaction" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.callsTo("DELETE", "/api/v1/transactions/t-1")).toHaveLength(1);
    expect(currentHref(router)).toBe("/transactions");
    expect(screen.queryByRole("group", { name: "Actions for 1 selected" })).not.toBeInTheDocument();
  });

  it("tags are changed in the drawer", async () => {
    const api = setup();
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    const tags = within(await panel.findByRole("group", { name: "Tags" }));
    await user.click(tags.getByRole("checkbox", { name: "trip" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")[0]?.body).toEqual({ tag_ids: ["t-1"] });
    });
    expect(tags.getByRole("checkbox", { name: "trip" })).toBeChecked();
    cleanup();

    // A viewer reads them.
    setup({ ...ROW, tag_ids: ["t-1"] }, "viewer");
    renderApp("/transactions?tx=t-1");
    const read = within(await drawer());
    expect(await read.findByText("trip")).toBeInTheDocument();
    expect(read.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("a new category unticks the second box, whose counts were for the old one", async () => {
    const api = setup();
    api.on("POST /api/v1/rules/merchant/preview", (call) =>
      json((call.body as { category_id: string }).category_id === "c-1" ? preview({ uncategorised: 3, guessed: 1 }) : preview()),
    );
    api.on("POST /api/v1/rules/merchant", () =>
      json({ rule: rule("r-9", "Quiet Bean Coffee: Groceries", "c-1"), replaced: false, previous_category_id: null, job_id: null }, 201),
    );
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.click(panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" }));
    await user.click(await panel.findByRole("checkbox", { name: "Also change the existing 22 transactions" }));
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    expect(await panel.findByRole("checkbox", { name: "Always use Groceries for Quiet Bean Coffee" })).toBeChecked();
    expect(await panel.findByRole("checkbox", { name: "Also change the existing 4 transactions" })).not.toBeChecked();
    await user.click(panel.getByRole("button", { name: "Save rule" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/rules/merchant").map((call) => call.body)).toEqual([
        { transaction_id: "t-1", category_id: "c-1", apply_to_existing: false },
      ]);
    });
  });

  it("a split row says each line has its own category", () => {
    const split = { ...ROW, category_id: null, category_source: null, category_confidence: null, splits: [] as typeof ROW.splits };
    expect(decidedBy({ ...split, splits: [{} as (typeof ROW.splits)[number]] }, undefined)).toBe("Split: each line has its own category");
    expect(decidedBy(split, undefined)).toBe("Nobody yet");
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

  it("a refused note and a suggestion already gone are said inside the drawer", async () => {
    const api = setup({ ...ROW, category_id: null, suggested_category_id: "c-1", category_source: "dictionary", category_confidence: "0.60" });
    api.on("PATCH /api/v1/transactions/:id", () => json({ type: "urn:perfin:problem:conflict", title: "Conflict", status: 409, detail: "The row changed meanwhile." }, 409));
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "accept_suggestions", changed: 0, results: [] }));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.click(panel.getByRole("button", { name: "Accept Groceries" }));
    expect(await panel.findByText("That suggestion is no longer there.")).toBeInTheDocument();
    await user.click(panel.getByRole("button", { name: "More actions for SQ *QUIET BEAN COFFEE 0042" }));
    await user.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: "Add note…" }));
    const note = within(await screen.findByRole("dialog", { name: "Note on SQ *QUIET BEAN COFFEE 0042" }));
    await user.type(note.getByLabelText("Note"), "lunch");
    await user.click(note.getByRole("button", { name: "Save note" }));
    expect(await panel.findByRole("alert")).toHaveTextContent("The row changed meanwhile.");
  });

  it("the rule's undo says inside the drawer whether it worked", async () => {
    const api = setup();
    api.on("POST /api/v1/rules/merchant", () =>
      json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: false, previous_category_id: null, job_id: null }, 201),
    );
    api.on("POST /api/v1/rules/:id/revert", () => json({ type: "urn:perfin:problem:conflict", title: "Conflict", status: 409, detail: "The rule changed meanwhile." }, 409));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.click(panel.getByRole("checkbox", { name: "Always use Dining for Quiet Bean Coffee" }));
    await user.click(await panel.findByRole("button", { name: "Save rule" }));
    const saved = within(await panel.findByRole("group", { name: "Rule saved" }));
    await user.click(saved.getByRole("button", { name: "Undo" }));
    expect(await saved.findByRole("alert")).toHaveTextContent("Could not undo the rule: The rule changed meanwhile.");
    api.on("POST /api/v1/rules/:id/revert", () => json({ rule_id: "r-9", reverted: 0, job_id: null }));
    await user.click(saved.getByRole("button", { name: "Undo" }));
    expect(await panel.findByText(/^Rule removed\./)).toBeInTheDocument();
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

  it("opened from the address, Escape still gives focus to the row", async () => {
    setup();
    const { router } = renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    await drawer();
    const open = await screen.findByRole("button", { name: "Open Quiet Bean Coffee, Nov 1, 2026, -$42.10" });
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(currentHref(router)).toBe("/transactions");
    await waitFor(() => {
      expect(open).toHaveFocus();
    });
  });

  it("a change the server refuses is put back and said inside the drawer", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => json({ type: "urn:perfin:problem:conflict", title: "Conflict", status: 409, detail: "The row changed meanwhile." }, 409));
    renderApp("/transactions?tx=t-1");
    const user = userEvent.setup();
    const panel = within(await drawer());
    await user.selectOptions(panel.getByLabelText("Category"), "c-1");
    // The toast region sits behind the modal drawer, so the drawer says it itself.
    expect(await panel.findByRole("alert")).toHaveTextContent("The row changed meanwhile.");
    expect(panel.getByLabelText("Category")).toHaveValue("c-2");
    expect(panel.getByText("Merchant list, 90%")).toBeInTheDocument();
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
