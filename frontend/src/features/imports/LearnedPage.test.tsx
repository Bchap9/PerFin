// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { lookupHandlers, NOW } from "../transactions/fixtures";
import type { LearnedLayout, LearnedWording, Mapping } from "./api";
import { layoutReads, mappingReads } from "./learned";

const TAUGHT: LearnedLayout = {
  id: "l-1",
  institution_id: "i-1",
  institution_name: "Quillfeather Mutual",
  kind: "statement",
  parser: "statement_generic",
  version: 2,
  learned_at: NOW,
  learned_from_document_id: "d-1",
  recognised_by: ["quillfeather mutual"],
  taught: true,
  fields: ["closing_balance", "period_end", "period_start"],
  tables: [{ kind: "transactions", columns: ["date", "payee", "amount"] }],
  ignored_lines: 1,
  notes: ["QF is the branch code"],
};

const NAMED: LearnedLayout = {
  ...TAUGHT,
  id: "l-2",
  institution_name: "Sample Card Company",
  version: 1,
  learned_from_document_id: null,
  recognised_by: ["sample card company"],
  taught: false,
  fields: [],
  tables: [],
  ignored_lines: 0,
  notes: [],
};

const MAPPING: Mapping = {
  id: "m-1",
  name: "Everyday export",
  account_id: "a-1",
  institution_id: null,
  header_fingerprint: "f".repeat(64),
  created_at: NOW,
  updated_at: NOW,
  created_by: null,
  last_used_at: NOW,
  use_count: 3,
  spec: {
    version: 1,
    skip_rows: 3,
    has_header: true,
    date_column: 0,
    date_format: "DD.MM.YYYY",
    debit_column: 5,
    credit_column: 6,
    decimal_separator: ",",
    sign: "inflow_positive",
    mode: "cash",
  },
};

function handlers(role: "owner" | "viewer" = "owner") {
  return {
    "GET /api/v1/auth/session": authedSession({ role }),
    ...lookupHandlers(),
    "GET /api/v1/imports/layouts": () => json({ items: [TAUGHT, NAMED] }),
    "GET /api/v1/imports/wording": () => json({ items: [WORDING, HEADING] }),
    "GET /api/v1/imports/mappings": () => json({ items: [MAPPING], next_cursor: null }),
  };
}

const WORDING: LearnedWording = {
  id: "a".repeat(32),
  layout: "fidelity-investments-statement",
  kind: "action",
  phrase: "periodic investment plan",
  meaning: "buy",
  learned_at: "2026-11-11T09:00:00Z",
  learned_from_document_id: "d-9",
};
const HEADING: LearnedWording = {
  id: "b".repeat(32),
  layout: "brokerage_statement_generic",
  kind: "section",
  phrase: "sweep program activity",
  meaning: "cash",
  learned_at: "2026-11-10T09:00:00Z",
  learned_from_document_id: null,
};

describe("what a learned layout and a mapping say they read", () => {
  it("names labelled facts, table columns and ignored lines in plain words", () => {
    expect(layoutReads(TAUGHT)).toEqual([
      "Labelled: closing balance, statement end, statement start.",
      "Transactions table: date, description, amount.",
      "1 line ignored.",
    ]);
    expect(layoutReads(NAMED)).toEqual(["Recognises the institution; the usual readers do the rest."]);
  });

  it("describes a mapping's dates, money columns and preamble", () => {
    expect(mappingReads(MAPPING)).toBe(
      "Reads dates as DD.MM.YYYY, separate money in and money out columns, decimal comma, 3 lines skipped above the header.",
    );
  });
});

describe("LearnedPage", () => {
  it("lists layouts and mappings with what they do", async () => {
    mockApi(handlers());
    renderApp("/documents/learned");
    const layouts = await screen.findByRole("table", { name: "Learned statement layouts" });
    expect(within(layouts).getByText("Quillfeather Mutual")).toBeInTheDocument();
    expect(within(layouts).getByText("Taught")).toBeInTheDocument();
    expect(within(layouts).getByText("Recognised")).toBeInTheDocument();
    expect(within(layouts).getByText('"quillfeather mutual"')).toBeInTheDocument();
    expect(within(layouts).getByText("Note: QF is the branch code")).toBeInTheDocument();
    const mappings = await screen.findByRole("table", { name: "Saved column mappings" });
    expect(within(mappings).getByText("Everyday export")).toBeInTheDocument();
    expect(within(mappings).getByText(/3 times, last/)).toBeInTheDocument();
  });

  it("lists the wording it learned in plain words and forgets a phrase", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/wording/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/documents/learned");
    const table = await screen.findByRole("table", { name: "Learned statement wording" });
    expect(within(table).getByText('"periodic investment plan"')).toBeInTheDocument();
    expect(within(table).getByText("A line that is a purchase")).toBeInTheDocument();
    expect(within(table).getByText("fidelity investments statement")).toBeInTheDocument();
    expect(
      within(table).getByText("A section of cash in or out, by the sign of the amount"),
    ).toBeInTheDocument();
    expect(screen.getByText(/No amount, date, security,\s+account or name from the statement is stored/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: 'Forget "periodic investment plan"' }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", `/api/v1/imports/wording/${"a".repeat(32)}`)).toHaveLength(1);
    });
  });

  it("forgets a layout after a confirmation", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/layouts/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/documents/learned");
    await user.click(await screen.findByRole("button", { name: "Forget the Quillfeather Mutual layout" }));
    const dialog = await screen.findByRole("dialog", { name: "Forget the Quillfeather Mutual layout?" });
    expect(api.callsTo("DELETE", "/api/v1/imports/layouts/:id")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Forget this layout" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/imports/layouts/l-1")).toHaveLength(1);
    });
    const [call] = api.callsTo("DELETE", "/api/v1/imports/layouts/l-1");
    expect(call?.headers.get("X-CSRF-Token")).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("shows the server's refusal and keeps the dialog open", async () => {
    const user = userEvent.setup();
    mockApi({
      ...handlers(),
      "DELETE /api/v1/imports/layouts/:id": () =>
        json({ type: "layout-not-found", title: "Not found", status: 404, detail: "No such learned layout." }, 404),
    });
    renderApp("/documents/learned");
    await user.click(await screen.findByRole("button", { name: "Forget the Sample Card Company layout" }));
    await user.click(screen.getByRole("button", { name: "Forget this layout" }));
    const dialog = screen.getByRole("dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("No such learned layout.");
    expect(dialog).toBeInTheDocument();
  });

  it("renames and deletes a column mapping", async () => {
    const user = userEvent.setup();
    const api = mockApi({
      ...handlers(),
      "PATCH /api/v1/imports/mappings/:id": (call) => json({ ...MAPPING, ...(call.body as object) }),
      "DELETE /api/v1/imports/mappings/:id": () => new Response(null, { status: 204 }),
    });
    renderApp("/documents/learned");
    await user.click(await screen.findByRole("button", { name: "Rename Everyday export" }));
    const name = screen.getByRole("textbox", { name: /Name/ });
    await user.clear(name);
    await user.click(screen.getByRole("button", { name: "Save name" }));
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
    await user.type(name, "Joint checking export");
    await user.click(screen.getByRole("button", { name: "Save name" }));
    await waitFor(() => {
      expect(api.callsTo("PATCH", "/api/v1/imports/mappings/m-1")).toHaveLength(1);
    });
    const [patch] = api.callsTo("PATCH", "/api/v1/imports/mappings/m-1");
    expect(patch?.body).toEqual({ name: "Joint checking export" });
    expect(patch?.headers.get("X-CSRF-Token")).toBeTruthy();

    await user.click(await screen.findByRole("button", { name: "Delete Everyday export" }));
    await user.click(screen.getByRole("button", { name: "Delete mapping" }));
    await waitFor(() => {
      expect(api.callsTo("DELETE", "/api/v1/imports/mappings/m-1")).toHaveLength(1);
    });
  });

  it("gives a viewer the lists without the buttons", async () => {
    mockApi(handlers("viewer"));
    renderApp("/documents/learned");
    await screen.findByRole("table", { name: "Learned statement layouts" });
    await screen.findByText("Everyday export");
    expect(screen.queryByRole("button", { name: /Forget/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Rename|Delete/ })).not.toBeInTheDocument();
  });
});
