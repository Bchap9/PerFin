// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { CATEGORIES, lookupHandlers, page, tx } from "./fixtures";
import { categoryRule, descriptionKey } from "./remember";

describe("descriptionKey", () => {
  it.each([
    ["Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731", "Corner Grocer Springfield IL"],
    ["OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE", "OAKFIELD RENTALS RENT JORDAN"],
    ["POS DEBIT 0042 QUIET BEAN COFFEE", "QUIET BEAN COFFEE"],
    ["Gusto Pay 123456 PPD ID: 9138864007", "Gusto Pay"],
    ["STREAMBOX *MONTHLY", "STREAMBOX MONTHLY"],
    ["", ""],
    [null, ""],
  ])("keeps what comes back every time: %s", (raw, expected) => {
    expect(descriptionKey(raw)).toBe(expected);
  });
});

describe("categoryRule", () => {
  it("only fills a missing category", () => {
    expect(categoryRule("  corner grocer ", "c-1", "Groceries")).toEqual({
      name: "Groceries: corner grocer",
      enabled: true,
      stop_processing: false,
      conditions: {
        type: "all",
        conditions: [{ type: "payee", op: "contains", value: "corner grocer" }, { type: "category_unset" }],
      },
      actions: [{ type: "set_category", category_id: "c-1" }],
    });
  });
});

const [FOOD] = CATEGORIES;
const FOOD_ID = FOOD?.id ?? "";
const FOOD_NAME = FOOD?.name ?? "";
const ROW = tx("t-1", { raw_payee: "Card Purchase 01/14 Corner Grocer #204", payee_id: null, category_id: FOOD_ID });

function handlers(role: "owner" | "member" = "owner") {
  return {
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/transactions": page([ROW]),
  };
}

describe("Always categorise like this", () => {
  it("saves a rule from the row and applies it to what has no category", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "POST /api/v1/rules/test": () => json({ items: [], matched: 4, scanned: 250, next_cursor: null }),
      "POST /api/v1/rules": (call) => json({ id: "r-9", ...(call.body as object) }, 201),
      "POST /api/v1/rules/apply": () => json({ job_id: "j-1" }, 202),
    });
    renderApp("/transactions");
    await user.click(await screen.findByRole("button", { name: /^Actions for/ }));
    await user.click(screen.getByRole("menuitem", { name: "Always categorise like this…" }));
    const dialog = await screen.findByRole("dialog", { name: "Always categorise like this" });
    const words = within(dialog).getByRole("textbox", { name: /When the description contains/ });
    expect(words).toHaveValue("Corner Grocer");
    expect(await within(dialog).findByText("4 transactions without a category match these words.")).toBeInTheDocument();
    // The dry run is sent what a rule does only (the server refuses a name there).
    const { conditions, actions, stop_processing } = categoryRule("Corner Grocer", FOOD_ID, FOOD_NAME);
    expect(api.callsTo("POST", "/api/v1/rules/test").at(-1)?.body).toEqual({
      rule: { conditions, actions, stop_processing },
      limit: 1,
      only_changes: false,
    });
    await user.click(within(dialog).getByRole("button", { name: "Save rule" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/rules/apply")).toHaveLength(1);
    });
    const [created] = api.callsTo("POST", "/api/v1/rules");
    expect(created?.headers.get("X-CSRF-Token")).toBeTruthy();
    expect(created?.body).toEqual(categoryRule("Corner Grocer", FOOD_ID, FOOD_NAME));
    expect(api.callsTo("POST", "/api/v1/rules/apply")[0]?.body).toEqual({ rule_ids: ["r-9"] });
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("shows the server's refusal", async () => {
    const user = userEvent.setup();
    mockApi({
      ...handlers(),
      "POST /api/v1/rules/test": () => json({ items: [], matched: 0, scanned: 0, next_cursor: null }),
      "POST /api/v1/rules": () => json({ type: "forbidden", title: "Forbidden", status: 403, detail: "Owners and admins only." }, 403),
    });
    renderApp("/transactions");
    await user.click(await screen.findByRole("button", { name: /^Actions for/ }));
    await user.click(screen.getByRole("menuitem", { name: "Always categorise like this…" }));
    const dialog = await screen.findByRole("dialog", { name: "Always categorise like this" });
    await user.click(within(dialog).getByRole("button", { name: "Save rule" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Owners and admins only.");
  });

  it("is not offered to someone who cannot write rules", async () => {
    const user = userEvent.setup();
    mockApi(handlers("member"));
    renderApp("/transactions");
    await user.click(await screen.findByRole("button", { name: /^Actions for/ }));
    expect(screen.getByRole("menuitem", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Always categorise like this…" })).not.toBeInTheDocument();
  });
});
