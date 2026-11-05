// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { json, mockApi, renderApp } from "../auth/testing";
import { CAT_DINING, CAT_FOOD, CAT_RENT, CATEGORIES, category, ownerSession, referenceHandlers } from "../planning/testing";
import { categoryLabels, categoryOptions, descendantIds, flattenCategories } from "./tree";

describe("category tree", () => {
  const cats = [...CATEGORIES, category("c-x", "Pizza", { parent_id: CAT_DINING }), category("c-orphan", "Lost", { parent_id: "gone" })].map((c) => c as (typeof CATEGORIES)[number] & { parent_id: string | null });

  it("orders parents before children and indents by depth", () => {
    expect(flattenCategories(cats as never).map((node) => `${String(node.depth)}:${node.category.name}`)).toEqual([
      "0:Food",
      "1:Dining",
      "2:Pizza",
      "0:Lost",
      "0:Rent",
    ]);
  });

  it("finds descendants, so a category cannot move under itself", () => {
    expect([...descendantIds(cats as never, CAT_FOOD)].sort()).toEqual([CAT_DINING, CAT_FOOD, "c-x"].sort());
    expect(categoryOptions(cats as never, (c) => !descendantIds(cats as never, CAT_FOOD).has(c.id)).map((o) => o.label)).toEqual(["Lost", "Rent"]);
  });

  it("labels with the full path", () => {
    expect(categoryLabels(cats as never).get("c-x")).toBe("Food > Dining > Pizza");
  });
});

describe("categories page", () => {
  it("merges a payee into another and sends the target id", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/payees": () =>
        json({
          items: [
            { id: "p-1", name: "Cafe Uno", normalized_name: "cafe uno", aliases: ["CAFE UNO #1"], default_category_id: CAT_DINING, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z" },
            { id: "p-2", name: "Cafe 1", normalized_name: "cafe 1", aliases: [], default_category_id: null, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z" },
          ],
          next_cursor: null,
        }),
      "POST /api/v1/payees/:id/merge": () => json({ id: "p-1", name: "Cafe Uno", normalized_name: "cafe uno", aliases: [], default_category_id: null, created_at: "2026-11-01T00:00:00Z", updated_at: "2026-11-01T00:00:00Z" }),
    });
    renderApp("/settings/categories");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Payees" }));
    await user.click(await screen.findByRole("button", { name: "Merge Cafe 1" }));
    const dialog = await screen.findByRole("dialog", { name: "Merge payee" });
    expect(within(dialog).getByRole("button", { name: "Merge" })).toBeDisabled();
    await user.selectOptions(within(dialog).getByLabelText("Merge into"), "p-1");
    await user.click(within(dialog).getByRole("button", { name: "Merge" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/payees/p-2/merge")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/payees/p-2/merge")[0]?.body).toEqual({ into: "p-1" });
  });

  it("adds a child category under a parent", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "POST /api/v1/categories": () => json(category("c-new", "Coffee", { parent_id: CAT_FOOD }), 201),
    });
    renderApp("/settings/categories");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add category" }));
    const dialog = await screen.findByRole("dialog", { name: "Add category" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Coffee");
    await user.selectOptions(within(dialog).getByLabelText("Parent category"), CAT_FOOD);
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/categories")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/categories")[0]?.body).toEqual({ name: "Coffee", parent_id: CAT_FOOD, kind: "expense", tax_treatment: null });
    expect(CAT_RENT).toBeDefined();
  });
});
