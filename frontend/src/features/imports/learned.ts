// SPDX-License-Identifier: AGPL-3.0-or-later
/** Plain words for what a learned layout or a saved column mapping does. */
import type { LearnedLayout, LearnedWording, Mapping } from "./api";

const FIELD_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  account_number: "account number",
  account_name: "account name",
  period_start: "statement start",
  period_end: "statement end",
  opening_balance: "opening balance",
  closing_balance: "closing balance",
  institution: "institution",
}));

const ROLE_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  date: "date",
  post_date: "posting date",
  payee: "description",
  memo: "memo",
  amount: "amount",
  debit: "money out",
  credit: "money in",
  balance: "balance",
  symbol: "symbol",
  security: "security name",
  quantity: "quantity",
  price: "price",
  market_value: "market value",
  cost_basis: "cost basis",
}));

const KIND_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  statement: "Statement",
  receipt: "Receipt",
  invoice: "Invoice",
  tax_form: "Tax form",
  payslip: "Payslip",
  insurance: "Insurance document",
  other: "Other document",
}));

const MEANING_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  buy: "a purchase",
  sell: "a sale",
  dividend: "a dividend",
  interest: "interest",
  fee: "a fee",
  reinvest: "a reinvestment",
  transfer_in: "shares moved in",
  transfer_out: "shares moved out",
  deposit: "a deposit",
  withdrawal: "a withdrawal",
  corporate_action: "a corporate action",
}));

const SECTION_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  income: "dividends and interest",
  deposit: "deposits",
  withdrawal: "withdrawals",
  card: "card purchases and checks",
  cash: "cash in or out, by the sign of the amount",
  fee: "fees and taxes",
  transfer_in: "shares moved in",
  transfer_out: "shares moved out",
}));

const COLUMN_LABELS: ReadonlyMap<string, string> = new Map(Object.entries({
  date: "the date",
  settle: "the settlement date",
  type: "the kind of transaction",
  description: "the description",
  symbol: "the symbol",
  quantity: "the quantity",
  price: "the price",
  amount: "the amount",
  fees: "the fees",
}));

/** What a learned phrase means, in plain words. */
export function wordingMeans(wording: LearnedWording): string {
  if (wording.kind === "column") {
    return `A table column holding ${COLUMN_LABELS.get(wording.meaning) ?? words(wording.meaning)}`;
  }
  if (wording.kind === "section") {
    return `A section of ${SECTION_LABELS.get(wording.meaning) ?? words(wording.meaning)}`;
  }
  return `A line that is ${MEANING_LABELS.get(wording.meaning) ?? words(wording.meaning)}`;
}

function words(code: string): string {
  return code.replaceAll("_", " ");
}

function list(items: readonly string[]): string {
  return items.join(", ");
}

export function kindLabel(kind: string): string {
  return KIND_LABELS.get(kind) ?? words(kind);
}

/** One sentence per thing the layout reads, for the "What it reads" column. */
export function layoutReads(layout: LearnedLayout): string[] {
  if (!layout.taught) {
    return ["Recognises the institution; the usual readers do the rest."];
  }
  const out: string[] = [];
  if (layout.fields.length > 0) {
    out.push(`Labelled: ${list(layout.fields.map((field) => FIELD_LABELS.get(field) ?? words(field)))}.`);
  }
  for (const table of layout.tables) {
    const columns = list(table.columns.map((role) => ROLE_LABELS.get(role) ?? words(role)));
    out.push(`${table.kind === "holdings" ? "Holdings" : "Transactions"} table: ${columns}.`);
  }
  if (layout.ignored_lines > 0) {
    out.push(layout.ignored_lines === 1 ? "1 line ignored." : `${String(layout.ignored_lines)} lines ignored.`);
  }
  return out.length > 0 ? out : ["Nothing labelled yet."];
}

/** The columns a saved mapping reads, in plain words. */
export function mappingReads(mapping: Mapping): string {
  const spec = mapping.spec;
  const parts: string[] = [`dates as ${spec.date_format}`];
  if (spec.amount_column !== null && spec.amount_column !== undefined) {
    parts.push(spec.indicator_column === null || spec.indicator_column === undefined ? "one amount column" : "an amount and a debit/credit marker");
  } else {
    parts.push("separate money in and money out columns");
  }
  if (spec.decimal_separator === ",") {
    parts.push("decimal comma");
  }
  if (spec.skip_rows > 0) {
    parts.push(spec.skip_rows === 1 ? "1 line skipped above the header" : `${String(spec.skip_rows)} lines skipped above the header`);
  }
  if (spec.account_column !== null && spec.account_column !== undefined) {
    parts.push("several accounts in one file");
  }
  return `Reads ${list(parts)}.`;
}
