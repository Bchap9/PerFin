// SPDX-License-Identifier: AGPL-3.0-or-later
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { csrfStore } from "../../lib/csrf";
import { queryRoots } from "../../lib/queryKeys";
import { expectNoAxeViolations } from "../../test/axe";
import { json, mockApi, problem } from "../auth/testing";
import { NewCategoryDialog } from "./NewCategoryDialog";

const STAMP = "2026-11-01T00:00:00Z";

function category(id: string, name: string, parent: string | null = null) {
  return {
    id,
    name,
    parent_id: parent,
    kind: "expense",
    system_key: null,
    tax_treatment: null,
    created_at: STAMP,
    updated_at: STAMP,
  };
}

const CATEGORIES = [
  category("c-food", "Food"),
  category("c-groceries", "Groceries", "c-food"),
  category("c-pets", "Pets"),
];

function renderDialog(props: { parentId?: string } = {}) {
  csrfStore.clear();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onCreated = vi.fn();
  const onClose = vi.fn();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <NewCategoryDialog open onClose={onClose} onCreated={onCreated} {...props} />
    </QueryClientProvider>,
  );
  return { onCreated, onClose, invalidate, ...view };
}

describe("NewCategoryDialog", () => {
  it("creates with name and parent", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }),
      "POST /api/v1/categories": () => json(category("c-new", "Treats", "c-pets"), 201),
    });
    const { onCreated, onClose, invalidate } = renderDialog();
    expect(screen.getByRole("dialog", { name: "New category" })).toBeInTheDocument();
    const parent = screen.getByRole("combobox", { name: "Inside" });
    // Only top-level categories can be the parent.
    await screen.findByRole("option", { name: "Pets" });
    expect(screen.queryByRole("option", { name: "Groceries" })).not.toBeInTheDocument();

    await user.type(screen.getByRole("textbox", { name: /Name/ }), "Treats");
    await user.selectOptions(parent, "c-pets");
    await user.click(screen.getByRole("button", { name: "Create category" }));

    await waitFor(() => {
      expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: "c-new", name: "Treats" }));
    });
    const posts = api.callsTo("POST", "/api/v1/categories");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toEqual({ name: "Treats", parent_id: "c-pets" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryRoots.taxonomy });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not submit a form it is mounted in", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }),
      "POST /api/v1/categories": () => json(category("c-new", "Garden"), 201),
    });
    const outer = vi.fn((event: { preventDefault: () => void }) => {
      event.preventDefault();
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <form onSubmit={outer}>
          <NewCategoryDialog open onClose={() => undefined} onCreated={() => undefined} />
        </form>
      </QueryClientProvider>,
    );
    await screen.findByRole("option", { name: "Pets" });
    await user.type(screen.getByRole("textbox", { name: /Name/ }), "Garden{Enter}");
    expect(outer).not.toHaveBeenCalled();
  });

  it("starts with the parent it is given, and sends no parent for a top-level category", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }),
      "POST /api/v1/categories": () => json(category("c-new", "Garden"), 201),
    });
    renderDialog({ parentId: "c-food" });
    await screen.findByRole("option", { name: "Pets" });
    const parent = screen.getByRole("combobox", { name: "Inside" });
    expect(parent).toHaveValue("c-food");
    await user.selectOptions(parent, "");
    await user.type(screen.getByRole("textbox", { name: /Name/ }), "Garden");
    await user.click(screen.getByRole("button", { name: "Create category" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/categories")).toHaveLength(1);
    });
    expect(api.callsTo("POST", "/api/v1/categories")[0]?.body).toEqual({ name: "Garden" });
  });

  it("shows the server's 422 under the field", async () => {
    const user = userEvent.setup();
    mockApi({
      "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }),
      "POST /api/v1/categories": () =>
        problem(422, "validation-error", {
          errors: [{ loc: ["body", "name"], msg: "Name must not be blank", type: "value_error" }],
        }),
    });
    const { onCreated } = renderDialog();
    await screen.findByRole("option", { name: "Pets" });
    await user.type(screen.getByRole("textbox", { name: /Name/ }), "   x");
    await user.click(screen.getByRole("button", { name: "Create category" }));
    const name = screen.getByRole("textbox", { name: /Name/ });
    await waitFor(() => {
      expect(name).toHaveAccessibleDescription("Name must not be blank");
    });
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("does not send an empty name", async () => {
    const user = userEvent.setup();
    const api = mockApi({ "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }) });
    renderDialog();
    await screen.findByRole("option", { name: "Pets" });
    await user.click(screen.getByRole("button", { name: "Create category" }));
    expect(screen.getByRole("textbox", { name: /Name/ })).toHaveAccessibleDescription("Enter a name.");
    expect(api.callsTo("POST", "/api/v1/categories")).toHaveLength(0);
  });

  it("has no axe violations", async () => {
    mockApi({ "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }) });
    const { container } = renderDialog();
    await screen.findByRole("option", { name: "Pets" });
    await expectNoAxeViolations(container);
  });
});
