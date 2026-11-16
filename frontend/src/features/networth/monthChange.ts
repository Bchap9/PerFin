// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The change of net worth this month, shared by Home and Accounts so both
 * say the same figure in the same words: the 12-month series (one point a
 * month), net worth now against the last point before the month began.
 */
import { api, unwrap } from "../../lib/api/client";
import { compareIsoDates, startOfMonth } from "../../lib/dates";
import { amountSign, formatMoney, isDecimalString, negateAmount, subtractAmounts } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";

export const netWorthSeriesKey = [...queryRoots.netWorth, "home-series"] as const;

export const getNetWorthSeries = () =>
  unwrap(api.GET("/api/v1/networth", { params: { query: { range: "1y", granularity: "month" } } }));

/**
 * Net worth now against the last point of the series dated before the
 * month began; `null` when the series does not reach back that far.
 */
export function netWorthChange(
  net: string,
  points: readonly { as_of: string; net: { amount: string } }[],
  today: string,
): string | null {
  const monthStart = startOfMonth(today);
  const before = points.filter((point) => compareIsoDates(point.as_of, monthStart) < 0).at(-1);
  if (before === undefined || !isDecimalString(net) || !isDecimalString(before.net.amount)) {
    return null;
  }
  return subtractAmounts(net, before.net.amount);
}

/**
 * "Up $250.00 this month", in words so colour is never the only signal.
 * Money down is not red (design note 2.1): it takes the neutral tone.
 */
export function netWorthChangeWords(change: string, currency: string): { text: string; tone: "up" | "neutral" } {
  const sign = amountSign(change);
  if (sign === 0) {
    return { text: "No change this month", tone: "neutral" };
  }
  const size = sign < 0 ? negateAmount(change) : change;
  return { text: `${sign > 0 ? "Up" : "Down"} ${formatMoney(size, currency) ?? `${size} ${currency}`} this month`, tone: sign > 0 ? "up" : "neutral" };
}
