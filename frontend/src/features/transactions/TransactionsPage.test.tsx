// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { account, lookupHandlers, page, tx } from "./fixtures";
import { authedSession, json, mockApi, problem, renderApp, type SessionOptions } from "../auth/testing";
import { patchPages } from "./optimistic";

function setup(role: SessionOptions["role"] = "owner", rows = [tx("x-1"), tx("x-2", { payee_id: null, raw_payee: "ACME", category_id: null })]) {
  return mockApi({
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": page(rows),
  });
}

describe("patchPages", () => {
  it("patches only the matching row", () => {
    const data = { pageParams: [null], pages: [{ items: [tx("a"), tx("b")], next_cursor: null }] };
    const next = patchPages(data, "b", { category_id: "c-2", tag_ids: ["t-1"] });
    expect(next?.pages[0]?.items.map((row) => row.category_id)).toEqual(["c-1", "c-2"]);
    expect(next?.pages[0]?.items[1]?.tag_ids).toEqual(["t-1"]);
    expect(data.pages[0]?.items[1]?.category_id).toBe("c-1");
  });
});

describe("transactions grid", () => {
  it("lists rows and passes filters to the API with a cursor for more", async () => {
    const api = setup("owner");
    let calls = 0;
    api.on("GET /api/v1/transactions", () => {
      calls += 1;
      return json({ items: [tx(`r-${String(calls)}`)], next_cursor: calls === 1 ? "cur-1" : null });
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    expect(await screen.findByLabelText("Category of Corner Market")).toHaveValue("c-1");
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => {
      expect(api.callsTo("GET", "/api/v1/transactions").some((call) => call.search.includes("cursor=cur-1"))).toBe(true);
    });
    await user.selectOptions(screen.getByLabelText("Account"), "a-1");
    await user.type(screen.getByLabelText("Search"), "market");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      const last = api.callsTo("GET", "/api/v1/transactions").at(-1);
      expect(last?.search).toContain("account=a-1");
      expect(last?.search).toContain("q=market");
    });
  });

  it("applies an inline edit at once and keeps it when the server accepts it", async () => {
    const api = setup();
    api.on("PATCH", () => json({}));
    let release: () => void = () => undefined;
    api.on("PATCH /api/v1/transactions/:id", async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return json(tx("x-1", { category_id: "c-2" }));
    });
    renderApp("/transactions");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Category of Corner Market");
    await user.selectOptions(select, "c-2");
    // Optimistic: the new value shows before the server has answered.
    expect(screen.getByLabelText("Category of Corner Market")).toHaveValue("c-2");
    release();
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/transactions/:id")).toHaveLength(1);
    });
    expect(api.callsTo("PATCH", "/api/v1/transactions/:id")[0]?.body).toEqual({ category_id: "c-2" });
  });

  it("rolls the cell back and says why when the server refuses the edit", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(404, "category-not-found"));
    renderApp("/transactions");
    const user = userEvent.setup();
    const select = await screen.findByLabelText("Category of Corner Market");
    // The refetch after the failure never answers, so only the rollback can restore the value.
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.selectOptions(select, "c-2");
    expect(await screen.findByText("Could not save the change")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Category of Corner Market")).toHaveValue("c-1");
    });
  });

  it("rolls back a note edit too", async () => {
    const api = setup();
    api.on("PATCH /api/v1/transactions/:id", () => problem(500, "internal-error"));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit notes of Corner Market" }));
    api.on("GET /api/v1/transactions", () => new Promise<Response>(() => undefined));
    await user.type(screen.getByLabelText("Notes of Corner Market"), "weekly shop{Enter}");
    expect(await screen.findByText("Could not save the change")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Edit notes of Corner Market" })).toHaveTextContent("Add note");
    });
  });

  it("gives a viewer a read-only grid", async () => {
    setup("viewer");
    renderApp("/transactions");
    const grid = await screen.findByRole("table", { name: "Transactions" });
    await within(grid).findByText("Groceries");
    expect(screen.queryByLabelText("Category of Corner Market")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Split/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByText("You have read-only access.")).toBeInTheDocument();
  });

  it("does not offer edits on an account the member may only view", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers([account("a-1", "Everyday", { can_edit: false })]),
      "GET /api/v1/transactions": page([tx("x-1")]),
    });
    renderApp("/transactions");
    await within(await screen.findByRole("table", { name: "Transactions" })).findByText("Groceries");
    expect(screen.queryByLabelText("Category of Corner Market")).not.toBeInTheDocument();
  });

  it("sends a bulk categorise for the selected rows", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/bulk", () => json({ action: "categorise", changed: 2, results: [] }));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByLabelText("Select all loaded transactions"));
    await user.selectOptions(screen.getByLabelText("Bulk category"), "c-2");
    await user.click(screen.getByRole("button", { name: "Set category" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/bulk")[0]?.body).toEqual({ action: "categorise", ids: ["x-1", "x-2"], category_id: "c-2" });
    });
    expect(await screen.findByText("2 transactions changed")).toBeInTheDocument();
  });
});

describe("split editor", () => {
  it("blocks saving until the lines add up, then posts decimal strings", async () => {
    const api = setup();
    api.on("POST /api/v1/transactions/:id/split", () => json(tx("x-1")));
    renderApp("/transactions");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Split Corner Market" }));
    const dialog = await screen.findByRole("dialog", { name: "Split transaction" });
    const first = within(within(dialog).getByRole("group", { name: "Split line 1" })).getByLabelText("Amount");
    const second = within(within(dialog).getByRole("group", { name: "Split line 2" })).getByLabelText("Amount");
    await user.clear(first);
    await user.type(first, "-40.00");
    await user.type(second, "-1.00");
    expect(within(dialog).getByRole("button", { name: "Save split" })).toBeDisabled();
    expect(within(dialog).getByText("The lines are off by -1.10.")).toBeInTheDocument();
    await user.clear(second);
    await user.type(second, "-2.10");
    expect(within(dialog).getByText("The lines add up.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Save split" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/transactions/:id/split")).toHaveLength(1);
    });
    const body = api.callsTo("POST", "/api/v1/transactions/:id/split")[0]?.body as { splits: { amount: unknown }[] };
    expect(body.splits.map((split) => split.amount)).toEqual([
      { amount: "-40.00", currency: "USD" },
      { amount: "-2.10", currency: "USD" },
    ]);
  });
});
