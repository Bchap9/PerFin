// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * How current the holdings screen is: what an account holds is known up to
 * its last import (a statement or a positions export), while prices may be
 * newer. Between imports a market value is the latest price times the units
 * of that import, an estimate. Pure helpers, no arithmetic on amounts.
 */
import { formatDate } from "../../lib/dates";
import type { Holding, Holdings } from "./api";

type Price = NonNullable<Holding["price"]>;

/** Where a holding's price came from, in a few words. */
export function priceSourceLabel(price: Price): string {
  if (price.basis === "trade") {
    return "last trade";
  }
  switch (price.source) {
    case "manual":
      return "entered by hand";
    case "statement":
      return "from an import";
    case null:
      return "stored price";
    default:
      return "price feed";
  }
}

/** "every 15 minutes", "every hour", "every 6 hours". */
export function refreshEvery(minutes: number): string {
  if (minutes % 60 !== 0) {
    return `every ${String(minutes)} minutes`;
  }
  const hours = minutes / 60;
  return hours === 1 ? "every hour" : `every ${String(hours)} hours`;
}

/** The earliest and latest "last import" days among the accounts that have one (ISO dates). */
export function importRange(accounts: Holdings["accounts"]): { oldest: string; newest: string } | null {
  const days = accounts
    .map((account) => account.positions_as_of)
    .filter((day): day is string => day !== null)
    .sort();
  const oldest = days[0];
  const newest = days[days.length - 1];
  return oldest === undefined || newest === undefined ? null : { oldest, newest };
}

/** The last import day of the account a holding is in (ISO date), if any. */
export function importedOn(data: Holdings, accountId: string): string | null {
  return data.accounts.find((account) => account.account_id === accountId)?.positions_as_of ?? null;
}

/** What the screen says about how current it is; null when nothing was imported. */
export function freshnessNote(data: Holdings): string | null {
  const range = importRange(data.accounts);
  if (range === null) {
    return null;
  }
  const day = (iso: string) => formatDate(iso) ?? iso;
  const known =
    range.oldest === range.newest
      ? `What you hold is as of your last import (${day(range.newest)}).`
      : `What you hold is as of each account's last import (${day(range.oldest)} to ${day(range.newest)}).`;
  const prices =
    data.price_feed.enabled && data.price_feed.refresh_minutes !== null
      ? `Prices are fetched ${refreshEvery(data.price_feed.refresh_minutes)}.`
      : "No price feed is set up, so prices are those of your imports and the ones entered by hand.";
  return `${known} ${prices} Market values and gains are estimates: the units and cost basis may have changed since that import. Import a new positions export or statement to bring them up to date.`;
}
