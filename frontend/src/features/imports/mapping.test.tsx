// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { account, lookupHandlers } from "../transactions/fixtures";
import { buildSpec, rolesFromSpec, stateFromSpec, withRole, type ColumnRole } from "./mapping";

const roles = (...items: ColumnRole[]): ColumnRole[] => items;

describe("mapping spec", () => {
  const base = stateFromSpec(null, 4, true);

  it("builds a spec from column roles", () => {
    const state = { ...base, roles: roles("date", "payee", "amount", "balance"), dateFormat: "MM/DD/YYYY" as const, currency: "usd" };
    expect(buildSpec(state)).toEqual({
      errors: [],
      spec: {
        version: 1,
        date_column: 0,
        date_format: "MM/DD/YYYY",
        has_header: true,
        skip_rows: 0,
        decimal_separator: ".",
        sign: "inflow_positive",
        outflow_values: [],
        amount_column: 2,
        debit_column: null,
        credit_column: null,
        balance_column: 3,
        payee_column: 1,
        memo_column: null,
        currency_column: null,
        indicator_column: null,
        currency: "USD",
      },
    });
  });

  it("supports debit and credit columns and an indicator column", () => {
    const split = buildSpec({ ...base, roles: roles("date", "debit", "credit", "ignore") });
    expect(split.spec).toMatchObject({ debit_column: 1, credit_column: 2, amount_column: null });
    const flagged = buildSpec({ ...base, roles: roles("date", "amount", "indicator", "ignore"), outflowValues: "DR, Debit" });
    expect(flagged.spec).toMatchObject({ indicator_column: 2, outflow_values: ["DR", "Debit"] });
  });

  it("refuses incomplete or contradictory mappings", () => {
    expect(buildSpec({ ...base, roles: roles("payee", "amount", "ignore", "ignore") }).errors).toEqual(["Choose the date column."]);
    expect(buildSpec({ ...base, roles: roles("date", "payee", "ignore", "ignore") }).errors[0]).toMatch(/amount column/);
    expect(buildSpec({ ...base, roles: roles("date", "amount", "debit", "credit") }).errors[0]).toMatch(/not both/);
    expect(buildSpec({ ...base, roles: roles("date", "amount", "indicator", "ignore") }).errors[0]).toMatch(/indicator values/);
    expect(buildSpec({ ...base, roles: roles("date", "amount", "ignore", "ignore"), currency: "EURO" }).errors[0]).toMatch(/currency/);
    expect(buildSpec({ ...base, roles: roles("date", "amount", "ignore", "ignore"), skipRows: 51 }).spec).toBeNull();
  });

  it("moves a role instead of duplicating it, and round-trips a spec", () => {
    expect(withRole(roles("date", "amount", "ignore"), 2, "date")).toEqual(["ignore", "amount", "date"]);
    expect(withRole(roles("date", "amount", "ignore"), 0, "ignore")).toEqual(["ignore", "amount", "ignore"]);
    const spec = buildSpec({ ...base, roles: roles("date", "payee", "amount", "memo") }).spec;
    expect(spec).not.toBeNull();
    if (spec !== null) {
      expect(rolesFromSpec(spec, 4)).toEqual(["date", "payee", "amount", "memo"]);
    }
  });
});

const PREVIEW = {
  document_id: "d-1",
  status: "needs_review",
  kind: "csv",
  account_id: null,
  matched_mapping_id: null,
  suggested_spec: null,
  last_import: null,
  statements: [],
  warnings: [],
  tables: [
    {
      index: 0,
      encoding: "utf-8",
      delimiter: ",",
      has_header: true,
      column_count: 3,
      row_count: 2,
      truncated: false,
      offset: 0,
      header_fingerprint: "f".repeat(64),
      rows: [
        ["Posted", "Description", "Amount"],
        ["2026-10-01", "CORNER MARKET", "-12.50"],
      ],
    },
  ],
};

describe("mapping editor", () => {
  it("saves the mapping built from the chosen columns, then applies it", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers([account("a-1", "Everyday")]),
      "GET /api/v1/imports/:id/preview": () => json(PREVIEW),
      "POST /api/v1/imports/mappings": () => json({ id: "m-1" }, 201),
      "POST /api/v1/imports/:id/apply-mapping": () => json({ document_id: "d-1", job_id: "j-1", state: "queued" }, 202),
    });
    renderApp("/documents/d-1/mapping");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Column 1 holds"), "date");
    await user.selectOptions(screen.getByLabelText("Column 2 holds"), "payee");
    await user.selectOptions(screen.getByLabelText("Column 3 holds"), "amount");
    await user.selectOptions(screen.getByLabelText("Account"), "a-1");
    await user.clear(screen.getByLabelText(/Mapping name/));
    await user.type(screen.getByLabelText(/Mapping name/), "Credit union");
    await user.click(screen.getByRole("button", { name: "Save mapping and import" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/imports/:id/apply-mapping")).toHaveLength(1);
    });
    const saved = api.callsTo("POST", "/api/v1/imports/mappings")[0]?.body as Record<string, unknown>;
    expect(saved).toMatchObject({
      name: "Credit union",
      document_id: "d-1",
      account_id: "a-1",
      spec: { date_column: 0, payee_column: 1, amount_column: 2, date_format: "YYYY-MM-DD", has_header: true },
    });
    expect(api.callsTo("POST", "/api/v1/imports/:id/apply-mapping")[0]?.body).toEqual({ mapping_id: "m-1", account_id: "a-1" });
  });

  it("shows what is missing instead of saving", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/imports/:id/preview": () => json(PREVIEW),
    });
    renderApp("/documents/d-1/mapping");
    const user = userEvent.setup();
    await screen.findByLabelText("Column 1 holds");
    await user.click(screen.getByRole("button", { name: "Save mapping and import" }));
    expect(await screen.findByText("Choose the date column.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/v1/imports/mappings")).toHaveLength(0);
  });

  it("starts from the server's suggested mapping", async () => {
    mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "member" }),
      ...lookupHandlers(),
      "GET /api/v1/imports/:id/preview": () =>
        json({
          ...PREVIEW,
          suggested_spec: { version: 1, date_column: 0, date_format: "DD/MM/YYYY", has_header: true, skip_rows: 0, decimal_separator: ",", sign: "inflow_positive", outflow_values: [], amount_column: 2, payee_column: 1 },
        }),
    });
    renderApp("/documents/d-1/mapping");
    expect(await screen.findByLabelText("Column 1 holds")).toHaveValue("date");
    expect(screen.getByLabelText("Column 3 holds")).toHaveValue("amount");
    expect(screen.getByLabelText("Date format")).toHaveValue("DD/MM/YYYY");
    expect(screen.getByLabelText("Decimal separator")).toHaveValue(",");
  });
});
