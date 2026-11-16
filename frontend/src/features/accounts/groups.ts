// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Accounts page's five groups (spec section 1.3): which account types
 * each holds, their totals as decimal strings, and the line under each
 * account saying how current its balance is.
 */
import { tryParseIsoDate } from "../../lib/dates";
import { sumMoney, type Money } from "../../lib/money";
import type { Account, AccountType } from "./api";

export type GroupId = "cash" | "credit" | "investments" | "loans" | "property";

export interface GroupDefinition {
  id: GroupId;
  label: string;
  types: readonly AccountType[];
}

export const GROUPS: readonly GroupDefinition[] = [
  { id: "cash", label: "Cash", types: ["checking", "savings", "cash"] },
  { id: "credit", label: "Cards and credit", types: ["credit_card", "line_of_credit"] },
  { id: "investments", label: "Investments", types: ["brokerage", "retirement"] },
  { id: "loans", label: "Loans", types: ["loan", "mortgage", "manual_liability"] },
  { id: "property", label: "Property and other", types: ["manual_asset"] },
];

export const GROUP_IDS: readonly GroupId[] = GROUPS.map((group) => group.id);

/** The group an account type belongs to. */
export function groupOf(type: AccountType): GroupId {
  return GROUPS.find((group) => group.types.includes(type))?.id ?? "cash";
}

export interface AccountGroupView {
  id: GroupId;
  label: string;
  /** The sum of the balances when every account shares one currency; otherwise null. */
  total: Money | null;
  accounts: Account[];
}

/** The group's total when every account shares one currency; otherwise none. */
export function groupTotal(accounts: readonly Account[]): Money | null {
  const first = accounts[0];
  if (first === undefined || accounts.some((account) => account.balance.currency !== first.balance.currency)) {
    return null;
  }
  return sumMoney(
    accounts.map((account) => account.balance),
    first.balance.currency,
  );
}

/** Open accounts in the five groups, in order, by name; closed accounts and empty groups are left out. */
export function groupAccounts(accounts: readonly Account[]): AccountGroupView[] {
  const open = accounts.filter((account) => !account.is_closed);
  return GROUPS.map((group) => {
    const members = open
      .filter((account) => group.types.includes(account.type))
      .sort((a, b) => a.name.localeCompare(b.name));
    return { id: group.id, label: group.label, total: groupTotal(members), accounts: members };
  }).filter((group) => group.accounts.length > 0);
}

/** A balance a person types in themselves: a manual account, a manual asset or liability. */
export function isEstimate(account: Pick<Account, "manual_balance" | "type">): boolean {
  return account.manual_balance || account.type === "manual_asset" || account.type === "manual_liability";
}

/**
 * How current the balance is: "Balance from Nov 12" (the year shown when it
 * is not this year), or "Your estimate, June 2026" for a balance a person
 * enters. The date is the account's `balance_date`.
 */
export function freshness(account: Pick<Account, "balance_date" | "manual_balance" | "type">, now: Date): string {
  const date = tryParseIsoDate(account.balance_date);
  if (date === null) {
    return isEstimate(account) ? "Your estimate" : "";
  }
  const instant = Date.UTC(date.year, date.month - 1, date.day);
  if (isEstimate(account)) {
    const month = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" }).format(instant);
    return `Your estimate, ${month}`;
  }
  const sameYear = date.year === now.getUTCFullYear();
  const day = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    timeZone: "UTC",
  }).format(instant);
  return `Balance from ${day}`;
}
