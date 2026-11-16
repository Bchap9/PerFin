// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The account hub's tabs (spec section 1.3). Every account has Overview,
 * Transactions and Documents. An investment account adds Holdings, Activity
 * and Performance; an account that carries a loan adds Schedule, Payments,
 * Rate changes and Payoff; a manual asset adds Valuations.
 *
 * The loan and asset tabs follow what exists, not only the type: a line of
 * credit with loan terms gets the loan tabs (the server allows a loan on it),
 * and a loan or mortgage without terms gets only Schedule, which offers to add
 * them, so no tab is ever empty without saying why.
 */
import type { Account, AccountType } from "./api";

export const HUB_TAB_IDS = [
  "overview",
  "transactions",
  "documents",
  "holdings",
  "activity",
  "performance",
  "schedule",
  "payments",
  "rates",
  "payoff",
  "valuations",
] as const;

export type HubTab = (typeof HUB_TAB_IDS)[number];

export const defaultTab: HubTab = "overview";

export interface HubTabItem {
  id: HubTab;
  label: string;
}

const LABELS: ReadonlyMap<HubTab, string> = new Map<HubTab, string>([
  ["overview", "Overview"],
  ["transactions", "Transactions"],
  ["documents", "Documents"],
  ["holdings", "Holdings"],
  ["activity", "Activity"],
  ["performance", "Performance"],
  ["schedule", "Schedule"],
  ["payments", "Payments"],
  ["rates", "Rate changes"],
  ["payoff", "Payoff"],
  ["valuations", "Valuations"],
]);

const INVESTMENT_TYPES: ReadonlySet<AccountType> = new Set<AccountType>(["brokerage", "retirement"]);
/** Types whose hub always offers the schedule, where missing terms are added. */
const LOAN_TYPES: ReadonlySet<AccountType> = new Set<AccountType>(["loan", "mortgage"]);
/** Types that may carry a loan (`LOAN_ACCOUNT_TYPES` on the server). */
const LOAN_CAPABLE: ReadonlySet<AccountType> = new Set<AccountType>(["loan", "mortgage", "line_of_credit"]);

/** Whether the hub must look for a loan on this account (`GET /loans`). */
export function mayHaveLoan(type: AccountType): boolean {
  return LOAN_CAPABLE.has(type);
}

/** Whether the hub must look for asset details on this account (`GET /assets`). */
export function mayHaveAsset(type: AccountType): boolean {
  return type === "manual_asset";
}

function item(id: HubTab): HubTabItem {
  return { id, label: LABELS.get(id) ?? id };
}

export function tabsFor(account: Pick<Account, "type">, found: { loanId?: string; assetId?: string } = {}): HubTabItem[] {
  const ids: HubTab[] = ["overview", "transactions", "documents"];
  if (INVESTMENT_TYPES.has(account.type)) {
    ids.push("holdings", "activity", "performance");
  }
  if (found.loanId !== undefined) {
    ids.push("schedule", "payments", "rates", "payoff");
  } else if (LOAN_TYPES.has(account.type)) {
    ids.push("schedule");
  }
  if (found.assetId !== undefined || mayHaveAsset(account.type)) {
    ids.push("valuations");
  }
  return ids.map(item);
}

const TYPE_LABELS: ReadonlyMap<AccountType, string> = new Map<AccountType, string>([
  ["checking", "Checking"],
  ["savings", "Savings"],
  ["cash", "Cash"],
  ["credit_card", "Credit card"],
  ["line_of_credit", "Line of credit"],
  ["brokerage", "Brokerage"],
  ["retirement", "Retirement"],
  ["loan", "Loan"],
  ["mortgage", "Mortgage"],
  ["manual_asset", "Asset"],
  ["manual_liability", "Liability"],
]);

/** The account's type in words, with its kind when it has one ("Retirement, Roth IRA"). */
export function typeLabel(account: Pick<Account, "type" | "subtype_label">): string {
  const base = TYPE_LABELS.get(account.type) ?? account.type;
  const kind = (account.subtype_label ?? "").trim();
  return kind === "" ? base : `${base}, ${kind}`;
}
