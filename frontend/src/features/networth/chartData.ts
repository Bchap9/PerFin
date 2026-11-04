// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Mapping API answers to chart data and breakdown rows. Exact amounts stay
 * decimal strings; the only numbers are the display-only plot values made
 * by `toSeriesPoints`.
 */
import { isZeroAmount, negateAmount } from "../../lib/money";
import type { NetWorthHistory, NetWorthLatest, NetWorthProjection } from "./api";
import { toSeriesPoints, type SeriesPoint } from "./SeriesChart";

/**
 * Assets, liabilities and net worth per bucket. Liabilities arrive as
 * positive amounts owed; they are plotted as given so the three lines read
 * as "what I own", "what I owe" and the difference.
 */
export function historyPoints(history: NetWorthHistory): SeriesPoint[] {
  return toSeriesPoints(
    history.points.map((point) => ({
      date: point.as_of,
      amounts: { assets: point.assets.amount, liabilities: point.liabilities.amount, net: point.net.amount },
    })),
  );
}

/** Net worth today and its trend/savings projections on one date axis. */
export function projectionPoints(projection: NetWorthProjection): SeriesPoint[] {
  const byDate = new Map<string, Record<string, string>>();
  const put = (date: string, key: string, amount: string): void => {
    byDate.set(date, { ...(byDate.get(date) ?? {}), [key]: amount });
  };
  put(projection.as_of, "trend", projection.current.amount);
  put(projection.as_of, "savings", projection.current.amount);
  for (const point of projection.trend?.points ?? []) {
    put(point.on, "trend", point.net.amount);
  }
  for (const point of projection.savings.points) {
    put(point.on, "savings", point.net.amount);
  }
  return toSeriesPoints(
    [...byDate.entries()]
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([date, amounts]) => ({ date, amounts })),
  );
}

export interface BreakdownRow {
  key: string;
  label: string;
  amount: string;
  currency: string;
}

const CLASS_LABELS: Record<string, string> = {
  cash: "Cash",
  investments: "Investments",
  real_estate: "Real estate",
  vehicles: "Vehicles",
  other_assets: "Other assets",
  credit_cards: "Credit cards",
  mortgages: "Mortgages",
  loans: "Loans",
  other_liabilities: "Other liabilities",
};

export function classLabel(key: string): string {
  return new Map(Object.entries(CLASS_LABELS)).get(key) ?? key.replace(/_/g, " ");
}

/** Class totals for one side, largest first by absolute amount is left to the server order; zero rows are dropped. */
export function classRows(totals: Readonly<Record<string, { amount: string; currency: string }>>): BreakdownRow[] {
  return Object.entries(totals)
    .filter(([, money]) => !isZeroAmount(money.amount))
    .map(([key, money]) => ({ key, label: classLabel(key), amount: money.amount, currency: money.currency }));
}

/** Accounts of one side with their base-currency value; accounts without a rate are left out. */
export function accountRows(latest: NetWorthLatest, side: "asset" | "liability"): BreakdownRow[] {
  return latest.accounts
    .filter((account) => account.side === side && account.amount_base !== null)
    .map((account) => {
      const base = account.amount_base;
      return {
        key: account.account_id,
        label: account.name,
        // Liabilities are signed negative in the account value; show what is owed as a positive amount.
        amount: side === "liability" && base?.amount.startsWith("-") === true ? negateAmount(base.amount) : (base?.amount ?? "0"),
        currency: base?.currency ?? latest.currency,
      };
    });
}
