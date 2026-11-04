// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { CAT_DINING, CAT_FOOD, PAYEE_CAFE, TAG_FUN, money, ownerSession, referenceHandlers } from "../planning/testing";

const STAMP = "2026-11-01T10:00:00Z";

const rule = (id: string, name: string, order: number, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  enabled: true,
  stop_processing: false,
  conditions: { type: "all", conditions: [{ type: "payee", op: "contains", value: name.toLowerCase() }] },
  actions: [{ type: "set_category", category_id: CAT_DINING }],
  hit_count: 3,
  last_hit_at: null,
  sort_order: order,
  redacted: false,
  created_at: STAMP,
  updated_at: STAMP,
  ...extra,
});

const RULES = [rule("r-1", "Alpha", 0), rule("r-2", "Beta", 1)];

describe("rules list", () => {
  it("shows the rules in run order and reorders with the full id list", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules": () => json({ items: RULES }),
      "POST /api/v1/rules/reorder": () => json({ items: [RULES[1], RULES[0]] }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    const table = await screen.findByRole("region", { name: "Rules in the order they run" });
    expect(within(table).getAllByRole("rowheader").map((cell) => cell.textContent)).toEqual(["Alpha", "Beta"]);
    expect(await within(table).findByText('Payee text contains "alpha"')).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Move Alpha up" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Move Beta up" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/rules/reorder")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/rules/reorder")[0]?.body).toEqual({ rule_ids: ["r-2", "r-1"] });
  });

  it("applies the rules to existing transactions and follows the job to the end", async () => {
    let polls = 0;
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules": () => json({ items: RULES }),
      "POST /api/v1/rules/apply": () => json({ job_id: "j-1", state: "queued" }, 202),
      "GET /api/v1/jobs/:id": () => {
        polls += 1;
        const finished = polls > 1;
        return json({
          id: "j-1",
          type: "run_rules",
          state: finished ? "succeeded" : "running",
          finished,
          attempts: 1,
          max_attempts: 3,
          created_at: STAMP,
          updated_at: STAMP,
        });
      },
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Apply to existing transactions" }));
    const dialog = await screen.findByRole("dialog", { name: "Apply rules to existing transactions" });
    await user.type(within(dialog).getByLabelText("From date"), "2026-10-01");
    await user.click(within(dialog).getByRole("button", { name: "Apply rules" }));
    expect(await within(dialog).findByText("Running")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/apply")[0]?.body).toEqual({ date_from: "2026-10-01" });
    expect(await within(dialog).findByText("Finished", undefined, { timeout: 4000 })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Done" })).toBeInTheDocument();
  });
});

describe("rule editor", () => {
  it("tests an unsaved rule against existing transactions and saves it", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "POST /api/v1/rules/test": () =>
        json({
          matched: 1,
          scanned: 10,
          next_cursor: null,
          items: [
            {
              matched_rule_ids: ["00000000-0000-0000-0000-000000000000"],
              skipped: [],
              changes: { category_id: { before: null, after: CAT_DINING }, added_tag_ids: [TAG_FUN] },
              transaction: {
                id: "t-1",
                account_id: "a-1",
                amount: money("-4.50"),
                posted_date: "2026-10-20",
                raw_payee: "CAFE UNO #12",
                memo: null,
                notes: null,
                category_id: null,
                payee_id: PAYEE_CAFE,
                review_state: "none",
                status: "cleared",
                tag_ids: [],
                splits: [],
                transfer: null,
                confidence: null,
                effective_date: null,
                source_document_id: null,
                created_at: STAMP,
                updated_at: STAMP,
              },
            },
          ],
        }),
      "POST /api/v1/rules": () => json(rule("r-9", "Coffee", 2), 201),
      "GET /api/v1/rules": () => json({ items: [] }),
    });
    renderApp("/rules/new");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^Rule name/), "Coffee");
    await user.type(screen.getByLabelText("Text 1"), "cafe");
    await user.selectOptions(await screen.findByLabelText("Category 1"), CAT_DINING);

    await user.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByText("1 of 10 transactions scanned would match.")).toBeInTheDocument();
    const matches = screen.getByRole("list", { name: "Matching transactions" });
    expect(within(matches).getByText(/CAFE UNO #12/)).toBeInTheDocument();
    expect(within(matches).getByText(/Category: none to Dining/)).toBeInTheDocument();
    expect(within(matches).getByText(/Tags added: fun/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/test")[0]?.body).toMatchObject({
      limit: 25,
      only_changes: true,
      rule: {
        stop_processing: false,
        conditions: { type: "all", conditions: [{ type: "payee", op: "contains", value: "cafe" }] },
        actions: [{ type: "set_category", category_id: CAT_DINING }],
      },
    });
    // Nothing is saved by a test.
    expect(api.callsTo("POST", "/api/v1/rules")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Save rule" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/rules")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/rules")[0]?.body).toMatchObject({ name: "Coffee", enabled: true, stop_processing: false });
    expect(await screen.findByRole("heading", { level: 1, name: "Rules" })).toBeInTheDocument();
  });

  it("shows what is wrong instead of saving an incomplete rule", async () => {
    const api = mockApi({ ...ownerSession(), ...referenceHandlers() });
    renderApp("/rules/new");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Save rule" }));
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
    expect(screen.getByText("Enter the text to look for.")).toBeInTheDocument();
    expect(screen.getByText("Choose a category.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Run test" })).toBeDisabled();
  });

  it("loads a stored rule into the builder", async () => {
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules/:id": () =>
        json(
          rule("r-1", "Alpha", 0, {
            conditions: { type: "any", conditions: [{ type: "not", condition: { type: "sign", sign: "inflow" } }] },
            actions: [{ type: "add_tags", tag_ids: [TAG_FUN] }, { type: "flag_for_review" }],
          }),
        ),
    });
    renderApp("/rules/r-1");
    expect(await screen.findByLabelText(/^Rule name/)).toHaveValue("Alpha");
    expect(screen.getByLabelText("Match")).toHaveValue("any");
    expect(screen.getByLabelText("Condition 1")).toHaveValue("sign");
    expect(screen.getByRole("checkbox", { name: "Not" })).toBeChecked();
    expect(screen.getByLabelText("Action 1")).toHaveValue("add_tags");
    expect(screen.getByLabelText("Action 2")).toHaveValue("flag_for_review");
    expect(CAT_FOOD).toBeDefined();
  });
});
