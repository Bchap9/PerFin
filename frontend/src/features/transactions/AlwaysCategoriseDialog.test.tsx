// SPDX-License-Identifier: AGPL-3.0-or-later
/** "Always categorise <merchant> as...": from the row menu, and offered after a correction in the grid. */
import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

import { authedSession, currentHref, json, mockApi, renderApp, type SessionOptions } from "../auth/testing";
import type { MerchantRulePreview } from "../rules/api";
import { account, lookupHandlers, NOW, page, tx } from "./fixtures";
import { forgetTeachDismissals } from "./TeachPrompt";

const MERCHANT = { raw_payee: "SQ *QUIET BEAN COFFEE 0042", payee_id: null, merchant_key: "quiet bean coffee", merchant: "Quiet Bean Coffee" };
const ROW = tx("t-1", { ...MERCHANT, category_id: "c-2", category_source: "person" });
const MENU_ITEM = "Always categorise Quiet Bean Coffee as…";

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

function setup(body: MerchantRulePreview = preview(), rows = [ROW], role: SessionOptions["role"] = "member") {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": page(rows),
    "POST /api/v1/rules/merchant/preview": () => json(body),
  });
}

async function openDialog(user: UserEvent): Promise<HTMLElement> {
  await user.click(await screen.findByRole("button", { name: /^Actions for/ }));
  await user.click(screen.getByRole("menuitem", { name: MENU_ITEM }));
  return screen.findByRole("dialog", { name: "Always categorise Quiet Bean Coffee" });
}

beforeEach(() => {
  forgetTeachDismissals();
});

describe("Always categorise a merchant, from the row", () => {
  it("the dialog states the three counts from the server", async () => {
    const api = setup();
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    // The merchant PerFin recognised and the bank's own wording.
    expect(within(dialog).getByText("SQ *QUIET BEAN COFFEE 0042")).toBeInTheDocument();
    // Preset to the row's category.
    expect(within(dialog).getByLabelText(/Set the category to/)).toHaveValue("c-2");
    expect(await within(dialog).findByText("17 transactions without a category")).toBeInTheDocument();
    expect(within(dialog).getByText("5 that PerFin guessed differently will change")).toBeInTheDocument();
    expect(within(dialog).getByText("6 that were chosen by hand or by another rule stay as they are")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview").at(-1)?.body).toEqual({ transaction_id: "t-1", category_id: "c-2" });
    // Changing what is already there is a separate choice, and not the default.
    expect(within(dialog).getByRole("checkbox", { name: "Also change the existing 22 transactions" })).not.toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Save rule" })).toBeEnabled();
  });

  it("says one in the singular and hides the box when nothing existing would change", async () => {
    setup(preview({ uncategorised: 0, guessed: 0, decided: 1, same_category: 0 }));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    expect(await within(dialog).findByText("1 that was chosen by hand or by another rule stays as it is")).toBeInTheDocument();
    expect(within(dialog).queryByText(/without a category/)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Save rule" })).toBeEnabled();
  });

  it("existing transactions are changed only when the box is ticked", async () => {
    const job = { id: "j-1", type: "rules.run", state: "running", finished: false, attempts: 1, max_attempts: 3, created_at: NOW, updated_at: NOW };
    for (const ticked of [false, true]) {
      const api = setup();
      api.on("POST /api/v1/rules/merchant", () =>
        json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: false, previous_category_id: null, job_id: ticked ? "j-1" : null }, 201),
      );
      api.on("GET /api/v1/jobs/:id", () => json(job));
      renderApp("/transactions");
      const user = userEvent.setup();
      const dialog = await openDialog(user);
      const box = await within(dialog).findByRole("checkbox", { name: "Also change the existing 22 transactions" });
      if (ticked) {
        await user.click(box);
      }
      await user.click(within(dialog).getByRole("button", { name: "Save rule" }));
      await waitFor(() => {
        expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      });
      const calls = api.callsTo("POST", "/api/v1/rules/merchant");
      expect(calls).toHaveLength(1);
      expect(calls[0]?.headers.get("X-CSRF-Token")).toBeTruthy();
      expect(calls[0]?.body).toEqual({ transaction_id: "t-1", category_id: "c-2", apply_to_existing: ticked });
      const saved = await screen.findByRole("group", { name: "Rule saved" });
      expect(within(saved).getByText("Quiet Bean Coffee is always Dining from now on.")).toBeInTheDocument();
      if (ticked) {
        // The job that changes the existing rows shows its progress.
        expect(await within(saved).findByText("Running")).toBeInTheDocument();
      } else {
        expect(api.callsTo("GET", "/api/v1/jobs/:id")).toHaveLength(0);
      }
      cleanup();
    }
  });

  it("a row that cannot teach says why and cannot be saved", async () => {
    const api = setup(preview({ teachable: false, reason: "not_a_purchase", uncategorised: 0, guessed: 0, decided: 0, same_category: 0 }));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    expect(await within(dialog).findByText(/This is not a purchase/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Save rule|Change the rule/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(0);
  });

  it("an earlier rule is named and nothing is saved", async () => {
    const api = setup(preview({ shadowed_by: "r-3" }));
    api.on("GET /api/v1/rules", () => json({ items: [rule("r-3", "Coffee shops", "c-1")] }));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    const link = await within(dialog).findByRole("link", { name: "Coffee shops" });
    expect(link).toHaveAttribute("href", "/rules/r-3");
    expect(link.closest("p")).toHaveTextContent("The rule Coffee shops already decides this merchant");
    expect(within(dialog).queryByRole("button", { name: /Save rule|Change the rule/ })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(0);
  });

  it("undo of a new rule calls revert", async () => {
    const fresh = setup();
    fresh.on("POST /api/v1/rules/merchant", () =>
      json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: false, previous_category_id: null, job_id: null }, 201),
    );
    fresh.on("POST /api/v1/rules/:id/revert", () => json({ rule_id: "r-9", reverted: 0, job_id: null }));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    expect(within(dialog).getByText(/and you can undo it/)).toBeInTheDocument();
    await user.click(await within(dialog).findByRole("button", { name: "Save rule" }));
    await user.click(await within(await screen.findByRole("group", { name: "Rule saved" })).findByRole("button", { name: "Undo" }));
    await waitFor(() => {
      expect(fresh.callsTo("POST", "/api/v1/rules/r-9/revert")).toHaveLength(1);
    });
    expect(fresh.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(1);
    await waitFor(() => {
      expect(screen.queryByRole("group", { name: "Rule saved" })).not.toBeInTheDocument();
    });
  });

  it("saving a rule that was there already with the same category offers no undo", async () => {
    // The server answers replaced:false for a rule it did not change; revert
    // would delete a rule this save did not make.
    const api = setup(preview({ existing_rule_id: "r-9" }));
    api.on("POST /api/v1/rules/merchant", () =>
      json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: false, previous_category_id: null, job_id: null }, 201),
    );
    api.on("POST /api/v1/rules/:id/revert", () => json({ rule_id: "r-9", reverted: 412, job_id: null }));
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    await user.click(await within(dialog).findByRole("button", { name: "Change the rule" }));
    const saved = await screen.findByRole("group", { name: "Rule saved" });
    expect(within(saved).getByRole("status")).toHaveTextContent("Quiet Bean Coffee is always Dining from now on. Quiet Bean Coffee had this rule already; it is as it was.");
    expect(within(saved).queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    await user.click(within(saved).getByRole("button", { name: "Hide" }));
    expect(api.callsTo("POST", "/api/v1/rules/:id/revert")).toHaveLength(0);
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(1);
  });

  it("a changed rule offers no undo and says how to go back", async () => {
    // Saving the earlier category again would also set this transaction to it
    // as the person's own choice, so it is not offered as an undo.
    const api = setup(preview({ existing_rule_id: "r-9" }));
    api.on("POST /api/v1/rules/merchant", () =>
      json({ rule: rule("r-9", "Quiet Bean Coffee: Dining", "c-2"), replaced: true, previous_category_id: "c-1", job_id: null }, 201),
    );
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    // The dialog does not promise an undo for a rule it only changes.
    expect(await within(dialog).findByRole("button", { name: "Change the rule" })).toBeInTheDocument();
    expect(within(dialog).queryByText(/you can undo it/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Change the rule" }));
    const saved = await screen.findByRole("group", { name: "Rule saved" });
    expect(within(saved).getByRole("status")).toHaveTextContent(
      "Quiet Bean Coffee is always Dining from now on. It was Groceries before. To go back, choose that category for the rule again.",
    );
    expect(within(saved).queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant").map((call) => call.body)).toEqual([
      { transaction_id: "t-1", category_id: "c-2", apply_to_existing: false },
    ]);
    expect(api.callsTo("POST", "/api/v1/rules/:id/revert")).toHaveLength(0);
  });

  it("shows the server's refusal and keeps the dialog open", async () => {
    const api = setup();
    api.on("POST /api/v1/rules/merchant", () =>
      json({ type: "urn:perfin:problem:rule-shadowed", title: "Conflict", status: 409, detail: "An earlier rule already decides this merchant." }, 409),
    );
    renderApp("/transactions");
    const user = userEvent.setup();
    const dialog = await openDialog(user);
    await user.click(await within(dialog).findByRole("button", { name: "Save rule" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("An earlier rule already decides this merchant.");
    expect(screen.queryByRole("group", { name: "Rule saved" })).not.toBeInTheDocument();
  });

  it("a member sees the menu item, a viewer does not", async () => {
    const rows = [
      ROW,
      // No merchant was recognised; a split row has no one category to teach.
      tx("t-2", { raw_payee: "CHECK 1042", payee_id: null, merchant_key: null, merchant: null }),
      tx("t-3", {
        raw_payee: "PLAIN LOAF BAKERY 77",
        payee_id: null,
        merchant_key: "plain loaf bakery",
        merchant: "Plain Loaf Bakery",
        category_id: null,
        splits: [
          { id: "s-1", amount: { amount: "-40.00", currency: "USD" }, category_id: "c-1", memo: null, tax_flag: false },
          { id: "s-2", amount: { amount: "-2.10", currency: "USD" }, category_id: "c-2", memo: null, tax_flag: false },
        ],
      }),
    ];
    setup(preview(), rows, "member");
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for SQ *QUIET BEAN COFFEE 0042" }));
    expect(screen.getByRole("menuitem", { name: MENU_ITEM })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    for (const other of ["CHECK 1042", "PLAIN LOAF BAKERY 77"]) {
      await user.click(screen.getByRole("button", { name: `Actions for ${other}` }));
      expect(screen.getByRole("menuitem", { name: "Edit" })).toBeInTheDocument();
      expect(screen.queryByRole("menuitem", { name: /^Always categorise/ })).not.toBeInTheDocument();
      await user.keyboard("{Escape}");
    }
    cleanup();

    setup(preview(), [ROW], "viewer");
    renderApp("/transactions");
    expect(await screen.findByText("SQ *QUIET BEAN COFFEE 0042")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Actions for/ })).not.toBeInTheDocument();
  });
});

describe("Offering the rule after a correction in the grid", () => {
  const UNSET = tx("t-1", { ...MERCHANT, category_id: null });
  const PROMPT = /from now on\?/;

  function grid(body: MerchantRulePreview, rows = [UNSET]) {
    const api = setup(body, rows);
    api.on("PATCH /api/v1/transactions/:id", (call) => json(tx("t-1", { ...MERCHANT, ...(call.body as object), category_source: "person" })));
    return api;
  }

  it('changing a category offers the rule once and "Not now" hides it', async () => {
    const api = grid(preview({ uncategorised: 3, guessed: 0, decided: 1, same_category: 1 }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Category of SQ *QUIET BEAN COFFEE 0042"), "c-1");
    const prompt = await screen.findByRole("group", { name: "Make it a rule?" });
    expect(within(prompt).getByText("Use Groceries for Quiet Bean Coffee from now on? 3 other transactions have no category yet.")).toBeInTheDocument();
    expect(screen.getAllByText(PROMPT)).toHaveLength(1);
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview").at(-1)?.body).toEqual({ transaction_id: "t-1", category_id: "c-1" });
    // "Always..." opens the dialog with the category just chosen; nothing is saved by asking.
    await user.click(within(prompt).getByRole("button", { name: "Always…" }));
    const dialog = await screen.findByRole("dialog", { name: "Always categorise Quiet Bean Coffee" });
    expect(within(dialog).getByLabelText(/Set the category to/)).toHaveValue("c-1");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(api.callsTo("POST", "/api/v1/rules/merchant")).toHaveLength(0);

    await user.click(within(screen.getByRole("group", { name: "Make it a rule?" })).getByRole("button", { name: "Not now" }));
    expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
    // Not asked again for this merchant: no prompt, and the server is not even asked.
    const asked = api.callsTo("POST", "/api/v1/rules/merchant/preview").length;
    await user.selectOptions(screen.getByLabelText("Category of SQ *QUIET BEAN COFFEE 0042"), "c-2");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/t-1")).toHaveLength(2);
    });
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").length).toBeGreaterThan(2);
    });
    expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(asked);
  });

  it("says so when the others carry a guess instead of nothing", async () => {
    grid(preview({ uncategorised: 0, guessed: 1, decided: 0, same_category: 1 }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Category of SQ *QUIET BEAN COFFEE 0042"), "c-1");
    expect(await screen.findByText("Use Groceries for Quiet Bean Coffee from now on? 1 other transaction has a different guess.")).toBeInTheDocument();
  });

  it("no prompt when nothing else would change", async () => {
    const cases: MerchantRulePreview[] = [
      preview({ uncategorised: 0, guessed: 0, decided: 4, same_category: 1 }),
      // The merchant has a rule already, an earlier rule decides it, or it cannot teach.
      preview({ existing_rule_id: "r-9" }),
      preview({ shadowed_by: "r-3" }),
      preview({ teachable: false, reason: "not_a_purchase" }),
    ];
    for (const body of cases) {
      const api = grid(body);
      renderApp("/transactions");
      const user = userEvent.setup();
      await user.selectOptions(await screen.findByLabelText("Category of SQ *QUIET BEAN COFFEE 0042"), "c-1");
      await waitFor(() => {
        expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).not.toHaveLength(0);
      });
      await waitFor(() => {
        expect(api.callsTo("GET", "/api/v1/transactions").length).toBeGreaterThan(1);
      });
      expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
      cleanup();
    }
  });

  it("no prompt for a cleared category or a row without a merchant", async () => {
    const api = grid(preview({ uncategorised: 3 }), [
      tx("t-1", { ...MERCHANT, category_id: "c-1" }),
      tx("t-2", { raw_payee: "CHECK 1042", payee_id: null, merchant_key: null, merchant: null, category_id: null }),
    ]);
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Category of SQ *QUIET BEAN COFFEE 0042"), "");
    await user.selectOptions(screen.getByLabelText("Category of CHECK 1042"), "c-1");
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(2);
    });
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").length).toBeGreaterThan(1);
    });
    expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(0);
  });
});

describe("Always categorise a merchant, from what PerFin learned", () => {
  it("a link with a transaction opens the dialog for that row", async () => {
    const api = setup(preview(), []);
    api.on("GET /api/v1/transactions/:id", () => json(ROW));
    renderApp("/transactions?always=t-1");
    const dialog = await screen.findByRole("dialog", { name: "Always categorise Quiet Bean Coffee" });
    expect(within(dialog).getByText("SQ *QUIET BEAN COFFEE 0042")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/transactions/t-1")).toHaveLength(1);
    // Closed, it stays closed.
    await userEvent.setup().click(await within(dialog).findByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("closing the dialog takes the transaction out of the address, and the other filters stay", async () => {
    const api = setup(preview(), []);
    api.on("GET /api/v1/transactions/:id", () => json(ROW));
    const { router } = renderApp("/transactions?always=t-1&category=none");
    const dialog = await screen.findByRole("dialog", { name: "Always categorise Quiet Bean Coffee" });
    await userEvent.setup().click(await within(dialog).findByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(currentHref(router)).toBe("/transactions?category=none");
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Replaced, not pushed: going back does not bring the dialog up again.
    expect(router.history.length).toBe(1);
  });

  it("the link does nothing for a row on an account the person may only view", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers([account("a-1", "Everyday", { can_edit: false }), account("a-2", "Spare")]),
      "GET /api/v1/transactions": page([]),
      "GET /api/v1/transactions/:id": () => json(ROW),
      "POST /api/v1/rules/merchant/preview": () => json(preview()),
    });
    renderApp("/transactions?always=t-1");
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions/t-1")).toHaveLength(1);
    });
    await screen.findByText(/No transactions/);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/merchant/preview")).toHaveLength(0);
  });

  it("the link does nothing for a viewer, a row that cannot teach or a row that is gone", async () => {
    const viewer = setup(preview(), [], "viewer");
    viewer.on("GET /api/v1/transactions/:id", () => json(ROW));
    renderApp("/transactions?always=t-1");
    await screen.findByText(/No transactions/);
    expect(viewer.callsTo("GET", "/api/v1/transactions/t-1")).toHaveLength(0);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    cleanup();

    const split = setup(preview(), []);
    split.on("GET /api/v1/transactions/:id", () => json({ ...ROW, merchant_key: null, merchant: null }));
    renderApp("/transactions?always=t-1");
    await waitFor(() => {
      expect(split.callsTo("GET", "/api/v1/transactions/t-1")).toHaveLength(1);
    });
    await screen.findByText(/No transactions/);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    cleanup();

    const gone = setup(preview(), []);
    renderApp("/transactions?always=t-1");
    await waitFor(() => {
      expect(gone.callsTo("GET", "/api/v1/transactions/t-1")).toHaveLength(1);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
