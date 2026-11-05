// SPDX-License-Identifier: AGPL-3.0-or-later
import { endOfMonth, startOfMonth } from "../../lib/dates";
import { sumAmounts } from "../../lib/money";
import type { Account } from "../planning/queries";

export function monthRange(today: string): { from: string; to: string } {
  return { from: startOfMonth(today), to: endOfMonth(today) };
}

/** Totals of open accounts per currency, assets and liabilities apart. */
export function accountTotals(accounts: readonly Account[]): { currency: string; assets: string; liabilities: string }[] {
  const byCurrency = new Map<string, { assets: string[]; liabilities: string[] }>();
  for (const account of accounts) {
    if (account.is_closed || !account.include_in_net_worth) {
      continue;
    }
    const entry = byCurrency.get(account.balance.currency) ?? { assets: [], liabilities: [] };
    (account.is_asset ? entry.assets : entry.liabilities).push(account.balance.amount);
    byCurrency.set(account.balance.currency, entry);
  }
  return [...byCurrency.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([currency, entry]) => ({ currency, assets: sumAmounts(entry.assets), liabilities: sumAmounts(entry.liabilities) }));
}
