// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { expectNoAxeViolations } from "../../test/axe";
import { authedSession, json, mockApi, renderApp } from "../auth/testing";
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
    await user.click(await screen.findByRole("button", { name: "New category" }));
    const dialog = await screen.findByRole("dialog", { name: "New category" });
    await user.type(within(dialog).getByLabelText(/^Name/), "Coffee");
    await user.selectOptions(within(dialog).getByLabelText("Inside"), CAT_FOOD);
    await user.click(within(dialog).getByRole("button", { name: "Create category" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/categories")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/categories")[0]?.body).toEqual({ name: "Coffee", parent_id: CAT_FOOD });
    expect(CAT_RENT).toBeDefined();
  });

  it("the four tabs are in the address", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { router } = renderApp("/settings/categories?tab=payees");
    const user = userEvent.setup();
    const tabs = await screen.findByRole("tablist", { name: "Categories, payees, tags and tax hints" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Categories", "Payees", "Tags", "Tax hints"]);
    expect(within(tabs).getByRole("tab", { name: "Payees" })).toHaveAttribute("aria-selected", "true");
    await user.click(within(tabs).getByRole("tab", { name: "Tags" }));
    await vi.waitFor(() => {
      expect(new URLSearchParams(router.state.location.searchStr).get("tab")).toBe("tags");
    });
    await user.click(within(tabs).getByRole("tab", { name: "Categories" }));
    await vi.waitFor(() => {
      expect(new URLSearchParams(router.state.location.searchStr).has("tab")).toBe(false);
    });
  });

  it("tax hints are a tab", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/tax-category-map": () =>
        json({ items: [{ category_id: CAT_RENT, category_name: "Rent", jurisdiction: "US", schedule: "Schedule E", line: "19", tax_treatment: null }] }),
      "PUT /api/v1/tax-category-map/:id": () =>
        json({ category_id: CAT_FOOD, category_name: "Food", jurisdiction: "US", schedule: "Schedule C", line: null, tax_treatment: null }),
    });
    renderApp("/settings/categories?tab=tax");
    const user = userEvent.setup();
    expect(await screen.findByRole("tab", { name: "Tax hints" })).toHaveAttribute("aria-selected", "true");
    // One page title; the tax hints are a panel of the tab, not a page of their own.
    expect(screen.getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["Settings"]);
    expect(screen.getByRole("heading", { level: 2, name: "Categories" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Schedule hints" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Schedule for Rent")).toHaveValue("Schedule E");
    await user.type(screen.getByLabelText("Schedule for Food"), "Schedule C");
    await user.click(screen.getByRole("button", { name: "Save hint for Food" }));
    await vi.waitFor(() => {
      expect(api.callsTo("PUT", `/api/v1/tax-category-map/${CAT_FOOD}`)).toHaveLength(1);
    });
  });

  it("a viewer sees no write buttons", async () => {
    const handlers = (role: "member" | "viewer") => ({
      ...referenceHandlers(),
      "GET /api/v1/auth/session": authedSession({ role }),
      "GET /api/v1/tax-category-map": () => json({ items: [] }),
    });
    mockApi(handlers("member"));
    const member = renderApp("/settings/categories");
    // A member adds and edits; merging and deleting are an owner's or admin's.
    expect(await screen.findByRole("button", { name: "Edit Food" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New category" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge Food" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete Rent" })).not.toBeInTheDocument();
    member.unmount();

    mockApi(handlers("viewer"));
    const viewer = renderApp("/settings/categories");
    expect(await screen.findByRole("rowheader", { name: "Rent" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New category" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Edit|Merge|Delete) / })).not.toBeInTheDocument();
    viewer.unmount();

    mockApi(handlers("viewer"));
    renderApp("/settings/categories?tab=tax");
    expect(await screen.findByRole("rowheader", { name: "Rent" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Save|Remove) hint/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /^Schedule for/ })).not.toBeInTheDocument();
  });

  it("payees and tags show each role only what the API lets it do", async () => {
    const as = (role: "member" | "viewer") => ({ ...referenceHandlers(), "GET /api/v1/auth/session": authedSession({ role }) });
    mockApi(as("member"));
    let view = renderApp("/settings/categories?tab=payees");
    expect(await screen.findByRole("button", { name: "Edit Cafe Uno" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add payee" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Merge|Delete) Cafe Uno$/ })).not.toBeInTheDocument();
    view.unmount();

    mockApi(as("member"));
    view = renderApp("/settings/categories?tab=tags");
    expect(await screen.findByRole("button", { name: "Rename fun" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add tag" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete fun" })).not.toBeInTheDocument();
    view.unmount();

    mockApi(as("viewer"));
    view = renderApp("/settings/categories?tab=payees");
    expect(await screen.findByRole("rowheader", { name: "Cafe Uno" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Add payee|Edit|Merge|Delete)/ })).not.toBeInTheDocument();
    view.unmount();

    mockApi(as("viewer"));
    renderApp("/settings/categories?tab=tags");
    expect(await screen.findByText("fun")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^(Add tag|Rename|Delete)/ })).not.toBeInTheDocument();
  });

  it("indentation without a style attribute", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { container } = renderApp("/settings/categories");
    const dining = await screen.findByText("Dining", { selector: "span" });
    expect(dining).toHaveClass("indent-1");
    expect(screen.getByText("Food", { selector: "span" })).not.toHaveClass("indent-1");
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("has no axe violations", async () => {
    mockApi({ ...ownerSession(), ...referenceHandlers() });
    const { container } = renderApp("/settings/categories");
    await screen.findByRole("rowheader", { name: "Dining" });
    await expectNoAxeViolations(container);
  });
});
