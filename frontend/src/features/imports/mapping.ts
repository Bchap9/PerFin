// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Column-mapping state of the CSV mapping editor and the `MappingSpec` it
 * produces (docs/INGESTION.md, "Delimited files"). Pure functions so the
 * output is testable without the UI.
 *
 * The "account" role marks a column of account numbers in an export that
 * lists several accounts (a brokerage activity file): the server splits the
 * rows by it and matches each account by its last four digits.
 */
import type { MappingSpec } from "./api";

export type ColumnRole = "ignore" | "date" | "payee" | "memo" | "amount" | "debit" | "credit" | "balance" | "currency" | "indicator" | "account";

export const COLUMN_ROLES: readonly { value: ColumnRole; label: string }[] = [
  { value: "ignore", label: "Ignore" },
  { value: "date", label: "Date" },
  { value: "payee", label: "Payee" },
  { value: "memo", label: "Memo" },
  { value: "amount", label: "Amount" },
  { value: "debit", label: "Debit (money out)" },
  { value: "credit", label: "Credit (money in)" },
  { value: "balance", label: "Balance" },
  { value: "currency", label: "Currency" },
  { value: "indicator", label: "Debit/credit indicator" },
  { value: "account", label: "Account number (several accounts)" },
];

export function isColumnRole(value: string): value is ColumnRole {
  return COLUMN_ROLES.some((role) => role.value === value);
}

export const DATE_FORMATS: readonly MappingSpec["date_format"][] = [
  "YYYY-MM-DD",
  "YYYY/MM/DD",
  "YYYY.MM.DD",
  "YYYYMMDD",
  "MM/DD/YYYY",
  "MM/DD/YY",
  "MM-DD-YYYY",
  "DD/MM/YYYY",
  "DD/MM/YY",
  "DD-MM-YYYY",
  "DD.MM.YYYY",
  "DD.MM.YY",
  "DD MMM YYYY",
  "DD-MMM-YYYY",
  "MMM DD, YYYY",
];

export function isDateFormat(value: string): value is MappingSpec["date_format"] {
  return (DATE_FORMATS as readonly string[]).includes(value);
}

export interface MappingState {
  /** Role per column index. */
  roles: readonly ColumnRole[];
  hasHeader: boolean;
  skipRows: number;
  dateFormat: MappingSpec["date_format"];
  decimalSeparator: "." | ",";
  sign: MappingSpec["sign"];
  /** Comma-separated values of the indicator column that mean money out. */
  outflowValues: string;
  /** Used when no currency column is mapped. */
  currency: string;
}

export interface BuildResult {
  spec: MappingSpec | null;
  errors: string[];
}

function indexOfRole(roles: readonly ColumnRole[], role: ColumnRole): number | null {
  const index = roles.indexOf(role);
  return index < 0 ? null : index;
}

/** The roles implied by an existing spec (e.g. the server's suggestion). */
export function rolesFromSpec(spec: MappingSpec, columnCount: number): ColumnRole[] {
  const roles: ColumnRole[] = Array.from({ length: columnCount }, () => "ignore");
  const assign = (index: number | null | undefined, role: ColumnRole): void => {
    if (index !== null && index !== undefined && index >= 0 && index < columnCount) {
      roles.splice(index, 1, role);
    }
  };
  assign(spec.date_column, "date");
  assign(spec.payee_column, "payee");
  assign(spec.memo_column, "memo");
  assign(spec.amount_column, "amount");
  assign(spec.debit_column, "debit");
  assign(spec.credit_column, "credit");
  assign(spec.balance_column, "balance");
  assign(spec.currency_column, "currency");
  assign(spec.indicator_column, "indicator");
  assign(spec.account_column, "account");
  return roles;
}

export function stateFromSpec(spec: MappingSpec | null, columnCount: number, hasHeader: boolean): MappingState {
  if (spec === null) {
    return {
      roles: Array.from({ length: columnCount }, () => "ignore" as const),
      hasHeader,
      skipRows: 0,
      dateFormat: "YYYY-MM-DD",
      decimalSeparator: ".",
      sign: "inflow_positive",
      outflowValues: "",
      currency: "",
    };
  }
  return {
    roles: rolesFromSpec(spec, columnCount),
    hasHeader: spec.has_header,
    skipRows: spec.skip_rows,
    dateFormat: spec.date_format,
    decimalSeparator: spec.decimal_separator,
    sign: spec.sign,
    outflowValues: (spec.outflow_values ?? []).join(", "),
    currency: spec.currency ?? "",
  };
}

/** Whether the rows name their own accounts (one statement per account number). */
export function hasAccountColumn(state: MappingState): boolean {
  return state.roles.includes("account");
}

/** Change one column's role; a role other than "ignore" belongs to one column, so it leaves any other. */
export function withRole(roles: readonly ColumnRole[], index: number, role: ColumnRole): ColumnRole[] {
  return roles.map((current, position) => {
    if (position === index) {
      return role;
    }
    return role !== "ignore" && current === role ? "ignore" : current;
  });
}

/** Validate the state and build the spec the API stores. */
export function buildSpec(state: MappingState): BuildResult {
  const errors: string[] = [];
  const date = indexOfRole(state.roles, "date");
  const amount = indexOfRole(state.roles, "amount");
  const debit = indexOfRole(state.roles, "debit");
  const credit = indexOfRole(state.roles, "credit");
  const indicator = indexOfRole(state.roles, "indicator");
  const currencyColumn = indexOfRole(state.roles, "currency");
  if (date === null) {
    errors.push("Choose the date column.");
  }
  if (amount === null && debit === null && credit === null) {
    errors.push("Choose an amount column, or debit and credit columns.");
  }
  if (amount !== null && (debit !== null || credit !== null)) {
    errors.push("Use either one amount column or debit and credit columns, not both.");
  }
  if ((debit === null) !== (credit === null) && amount === null) {
    errors.push("Debit and credit columns come as a pair.");
  }
  const outflow = state.outflowValues
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value !== "");
  if (indicator !== null && outflow.length === 0) {
    errors.push("List the indicator values that mean money out, such as DR.");
  }
  if (outflow.length > 10 || outflow.some((value) => value.length > 20)) {
    errors.push("Use at most 10 indicator values of up to 20 characters.");
  }
  const currency = state.currency.trim().toUpperCase();
  if (currencyColumn === null && currency !== "" && !/^[A-Z]{3}$/.test(currency)) {
    errors.push("Use a three-letter currency code.");
  }
  if (!Number.isInteger(state.skipRows) || state.skipRows < 0 || state.skipRows > 50) {
    errors.push("Skip between 0 and 50 rows.");
  }
  if (errors.length > 0 || date === null) {
    return { spec: null, errors };
  }
  const spec: MappingSpec = {
    version: 1,
    date_column: date,
    date_format: state.dateFormat,
    has_header: state.hasHeader,
    skip_rows: state.skipRows,
    decimal_separator: state.decimalSeparator,
    sign: state.sign,
    outflow_values: indicator === null ? [] : outflow,
    amount_column: amount,
    debit_column: debit,
    credit_column: credit,
    balance_column: indexOfRole(state.roles, "balance"),
    payee_column: indexOfRole(state.roles, "payee"),
    memo_column: indexOfRole(state.roles, "memo"),
    currency_column: currencyColumn,
    indicator_column: indicator,
    account_column: indexOfRole(state.roles, "account"),
    currency: currencyColumn === null && currency !== "" ? currency : null,
  };
  return { spec, errors: [] };
}
