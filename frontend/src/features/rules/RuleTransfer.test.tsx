// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { CAT_DINING, ownerSession, referenceHandlers } from "../planning/testing";
import { importProblem, parseRulesFile, readFileText, referenceSummary, rulesFilename } from "./portable";
import { problemFromBody } from "../../lib/api/client";

const saved = vi.hoisted(() => ({ saveBlob: vi.fn() }));
vi.mock("../settings/saveFile", async (importOriginal) => {
  const original = await importOriginal<typeof import("../settings/saveFile")>();
  return { ...original, saveBlob: saved.saveBlob };
});

const STAMP = "2026-11-01T10:00:00Z";
const NIL = "00000000-0000-0000-0000-000000000000";

const stored = (id: string, name: string, order: number, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  enabled: true,
  stop_processing: false,
  conditions: { type: "payee", op: "contains", value: name.toLowerCase() },
  actions: [{ type: "set_category", category_id: CAT_DINING }],
  hit_count: 0,
  last_hit_at: null,
  sort_order: order,
  redacted: false,
  created_at: STAMP,
  updated_at: STAMP,
  ...extra,
});

const portable = (name: string, extra: Record<string, unknown> = {}) => ({
  name,
  enabled: true,
  stop_processing: false,
  conditions: { type: "payee", op: "contains", value: name.toLowerCase() },
  actions: [{ type: "set_category", category_id: CAT_DINING }],
  ...extra,
});

const EXPORT = {
  format: "perfin-rules",
  version: 1,
  rules: [portable("Coffee"), portable("Fuel", { enabled: false })],
  references: { categories: [{ id: CAT_DINING, name: "Food / Dining" }], payees: [], tags: [], accounts: [] },
};

const RULES = [stored("r-1", "Alpha", 0), stored("r-2", "Beta", 1)];

const rulesFile = (content: unknown, name = "rules.json") =>
  new File([typeof content === "string" ? content : JSON.stringify(content)], name, { type: "application/json" });

describe("rules file checks", () => {
  it("accepts an export and counts what it holds", () => {
    const parsed = parseRulesFile(JSON.stringify(EXPORT));
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.file.names).toEqual(["Coffee", "Fuel"]);
      expect(parsed.file.enabled).toBe(1);
      expect(parsed.file.references).toEqual({ categories: 1, payees: 0, tags: 0, accounts: 0 });
      expect(parsed.file.hiddenAccount).toBe(false);
      expect(parsed.file.document).toEqual(EXPORT);
    }
    expect(referenceSummary({ categories: 2, payees: 1, tags: 0, accounts: 1 })).toBe("2 categories, 1 payee and 1 account");
    expect(referenceSummary({ categories: 0, payees: 0, tags: 0, accounts: 0 })).toBeNull();
    expect(rulesFilename(new Date("2026-11-10T08:00:00Z"))).toBe("perfin-rules-2026-11-10.json");
  });

  it("says in plain words why a file is not a rules export", () => {
    const message = (text: string): string => {
      const parsed = parseRulesFile(text);
      return parsed.ok ? "accepted" : parsed.message;
    };
    expect(message("{ not json")).toMatch(/not valid JSON/);
    expect(message(JSON.stringify([1, 2]))).toMatch(/does not look like a PerFin rules export/);
    expect(message(JSON.stringify({ transactions: [] }))).toMatch(/does not look like a PerFin rules export/);
    expect(message(JSON.stringify({ ...EXPORT, version: 2 }))).toMatch(/version 2/);
    expect(message(JSON.stringify({ ...EXPORT, rules: "none" }))).toMatch(/no list of rules/);
    expect(message(JSON.stringify({ ...EXPORT, rules: [] }))).toMatch(/holds no rules/);
    expect(message(JSON.stringify({ ...EXPORT, rules: [portable("Ok"), { name: "Broken" }] }))).toMatch(/Rule 2 in this file is incomplete/);
    expect(message(JSON.stringify({ ...EXPORT, references: { categories: [{ id: 1 }] } }))).toMatch(/names the rules refer to is damaged/);
    expect(message(JSON.stringify({ ...EXPORT, rules: Array.from({ length: 501 }, (_, index) => portable(`R${String(index)}`)) }))).toMatch(
      /at most 500/,
    );
  });

  it("drops members the server would not know and flags a hidden account", () => {
    const parsed = parseRulesFile(
      JSON.stringify({
        ...EXPORT,
        mode: "replace",
        exported_by: "someone",
        rules: [portable("Card", { conditions: { type: "account", account_ids: [NIL] } })],
      }),
    );
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(Object.keys(parsed.file.document).sort()).toEqual(["format", "references", "rules", "version"]);
      expect(parsed.file.hiddenAccount).toBe(true);
    }
  });

  it("names the rule a validation refusal is about", () => {
    const refusal = problemFromBody(422, {
      type: "urn:perfin:problem:validation-failed",
      title: "Request validation failed",
      detail: "One or more fields are invalid.",
      errors: [{ loc: ["body", "rules", 1, "actions", 0, "category_id"], msg: "Field required", type: "missing" }],
    });
    expect(importProblem(refusal, ["Coffee", "Fuel"])).toBe('Rule 2 ("Fuel") is not valid here (actions, category_id): Field required.');
    expect(importProblem(problemFromBody(409, { type: "urn:perfin:problem:rule-limit", title: "Too many rules", detail: "At most 500." }), [])).toBe(
      "At most 500.",
    );
  });
});

describe("export rules", () => {
  it("downloads the export document as a dated JSON file", async () => {
    saved.saveBlob.mockClear();
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules/export": () => json(EXPORT),
      "GET /api/v1/rules": () => json({ items: RULES }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await screen.findByRole("rowheader", { name: "Alpha" });
    await user.click(screen.getByRole("button", { name: "Export rules" }));
    expect(await screen.findByText("2 rules exported")).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/rules/export")).toHaveLength(1);
    expect(saved.saveBlob).toHaveBeenCalledTimes(1);
    const [blob, filename] = saved.saveBlob.mock.calls[0] as [Blob, string];
    expect(filename).toMatch(/^perfin-rules-\d{4}-\d{2}-\d{2}\.json$/);
    expect(blob.type).toBe("application/json");
    expect(JSON.parse(await readFileText(blob))).toEqual(EXPORT);
  });

  it("offers a member the export but not the import, and warns about hidden accounts", async () => {
    saved.saveBlob.mockClear();
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...referenceHandlers(),
      "GET /api/v1/rules/export": () => json(EXPORT),
      "GET /api/v1/rules": () => json({ items: [stored("r-1", "Alpha", 0, { redacted: true }), stored("r-2", "Beta", 1)] }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await screen.findByRole("rowheader", { name: "Beta" });
    expect(screen.queryByRole("button", { name: "Import rules" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Export rules" }));
    expect(await screen.findByText(/1 rule refers to accounts you cannot see/)).toBeInTheDocument();
    expect(saved.saveBlob).toHaveBeenCalledTimes(1);
  });

  it("says so when the export fails and saves nothing", async () => {
    saved.saveBlob.mockClear();
    mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules/export": () =>
        json({ type: "urn:perfin:problem:forbidden", title: "Forbidden", status: 403, detail: "You are not a member of a household." }, 403),
      "GET /api/v1/rules": () => json({ items: RULES }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await screen.findByRole("rowheader", { name: "Alpha" });
    await user.click(screen.getByRole("button", { name: "Export rules" }));
    expect(await screen.findByText("Rules not exported")).toBeInTheDocument();
    expect(screen.getByText("You are not a member of a household.")).toBeInTheDocument();
    expect(saved.saveBlob).not.toHaveBeenCalled();
  });
});

describe("import rules", () => {
  it("shows what the file holds, adds its rules after the existing ones and reports the result", async () => {
    let rules = RULES;
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "POST /api/v1/rules/import": () => {
        rules = [...RULES, stored("r-3", "Coffee", 2), stored("r-4", "Fuel", 3, { enabled: false })];
        return json({ items: rules }, 201);
      },
      "GET /api/v1/rules": () => json({ items: rules }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Import rules" }));
    const dialog = await screen.findByRole("dialog", { name: "Import rules" });
    // Nothing can be sent before a file is chosen.
    expect(within(dialog).getByRole("button", { name: "Import rules" })).toBeDisabled();
    await user.upload(within(dialog).getByLabelText("Rules file"), rulesFile(EXPORT, "from-home.json"));

    expect(await within(dialog).findByText(/holds 2 rules \(1 switched off; they stay off\)/)).toBeInTheDocument();
    const listed = within(dialog).getByRole("list", { name: "Rules in the file" });
    expect(within(listed).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["Coffee", "Fuel"]);
    expect(within(dialog).getByText(/The file names 1 category for the rules to be matched with yours/)).toBeInTheDocument();
    expect(within(dialog).getByText(/What to do with the rules you already have \(2\)/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Nothing is created/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText(/Keep them and add the file's rules after them/)).toBeChecked();
    expect(api.callsTo("POST", "/api/v1/rules/import")).toHaveLength(0);

    await user.click(within(dialog).getByRole("button", { name: "Add 2 rules" }));
    const done = await screen.findByRole("dialog", { name: "Rules imported" });
    expect(within(done).getByText(/Imported 2 rules\. You now have 4 rules\./)).toBeInTheDocument();
    const calls = api.callsTo("POST", "/api/v1/rules/import");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toEqual({ ...EXPORT, mode: "append" });
    expect(calls[0]?.headers.get("X-CSRF-Token")).toBe("session-token");
    expect(calls[0]?.headers.get("Idempotency-Key")).toMatch(/^[\x21-\x7e]{16,255}$/);

    await user.click(within(done).getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("rowheader", { name: "Coffee" })).toBeInTheDocument();
  });

  it("shows the server's refusal, retries with the same key and replaces with a new one", async () => {
    let refuse = true;
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "POST /api/v1/rules/import": () =>
        refuse
          ? json(
              {
                type: "urn:perfin:problem:rule-reference-unresolved",
                title: "Rule references could not be resolved",
                status: 422,
                detail: "Create these first or remove them from the rules: category 'Food / Dining'.",
              },
              422,
            )
          : json({ items: [stored("r-3", "Coffee", 0), stored("r-4", "Fuel", 1)] }, 201),
      "GET /api/v1/rules": () => json({ items: RULES }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Import rules" }));
    const dialog = await screen.findByRole("dialog", { name: "Import rules" });
    await user.upload(within(dialog).getByLabelText("Rules file"), rulesFile(EXPORT));
    await user.click(await within(dialog).findByRole("button", { name: "Add 2 rules" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Create these first or remove them from the rules: category 'Food / Dining'.",
    );
    // Still on the form: nothing was imported.
    expect(screen.queryByRole("dialog", { name: "Rules imported" })).not.toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Add 2 rules" }));
    await vi.waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/rules/import")).toHaveLength(2);
    });
    const key = (index: number) => api.callsTo("POST", "/api/v1/rules/import").at(index)?.headers.get("Idempotency-Key");
    expect(key(1)).toBe(key(0));

    refuse = false;
    await user.click(within(dialog).getByLabelText(/Delete them and use only the file's rules/));
    expect(within(dialog).getByText(/2 rules are deleted for good/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Replace my rules with 2 rules" }));
    const done = await screen.findByRole("dialog", { name: "Rules imported" });
    expect(within(done).getByText(/Imported 2 rules, replacing the 2 rules you had\. You now have 2 rules\./)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/import").at(2)?.body).toEqual({ ...EXPORT, mode: "replace" });
    expect(key(2)).not.toBe(key(0));
  });

  it("refuses a file that is not a rules export without sending anything", async () => {
    const api = mockApi({
      ...ownerSession(),
      ...referenceHandlers(),
      "GET /api/v1/rules": () => json({ items: RULES }),
    });
    renderApp("/rules");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Import rules" }));
    const dialog = await screen.findByRole("dialog", { name: "Import rules" });
    const input = within(dialog).getByLabelText("Rules file");
    await user.upload(input, rulesFile("date,payee,amount\n2026-01-01,Cafe,-4.50\n", "statement.json"));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This file is not valid JSON, so it cannot be a rules export.");
    expect(within(dialog).getByRole("button", { name: "Import rules" })).toBeDisabled();
    await user.upload(input, rulesFile({ accounts: [] }, "backup.json"));
    expect(await within(dialog).findByText(/does not look like a PerFin rules export/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/rules/import")).toHaveLength(0);
  });
});
