// SPDX-License-Identifier: AGPL-3.0-or-later
/** Report fixtures shared by the reports tests (imported by tests only). */
import { CAT_FOOD, CAT_RENT, money } from "../planning/testing";
import type { CashflowReport, FlowReport, TrendReport, TrendRow } from "./api";

export const SAVINGS = "a0000000-0000-4000-8000-000000000002";

export function flowReport(patch: Partial<FlowReport> = {}): FlowReport {
  return {
    flow: "spending",
    currency: "USD",
    date_from: "2026-11-01",
    date_to: "2026-11-30",
    group_by: "category",
    depth: 1,
    include_pending: false,
    items: [
      { key: CAT_RENT, name: "Rent", amount: money("1450.00") },
      { key: CAT_FOOD, name: "Food", amount: money("300.50") },
      { key: null, name: null, amount: money("49.50") },
    ],
    total: money("1800.00"),
    unconverted: [],
    ...patch,
  };
}

export function cashflowReport(patch: Partial<CashflowReport> = {}): CashflowReport {
  return {
    currency: "USD",
    date_from: "2026-10-01",
    date_to: "2026-11-30",
    include_pending: false,
    months: [
      { month: "2026-10", inflow: money("3466.18"), outflow: money("3667.08"), net: money("-200.90") },
      { month: "2026-11", inflow: money("3000.00"), outflow: money("2323.59"), net: money("676.41") },
    ],
    inflow: money("6466.18"),
    outflow: money("5990.67"),
    net: money("475.51"),
    unconverted: [],
    ...patch,
  };
}

export function trendRow(key: string | null, name: string | null, amounts: { now: string; previous: string; yearAgo: string }, percents: { mom: string | null; yoy: string | null }): TrendRow {
  const change = (base: string): string => (Number(amounts.now) - Number(base)).toFixed(2);
  return {
    key,
    name,
    amount: money(amounts.now),
    previous: money(amounts.previous),
    mom_change: money(change(amounts.previous)),
    mom_percent: percents.mom,
    year_ago: money(amounts.yearAgo),
    yoy_change: money(change(amounts.yearAgo)),
    yoy_percent: percents.yoy,
  };
}

export function trendReport(patch: Partial<TrendReport> = {}): TrendReport {
  return {
    flow: "spending",
    currency: "USD",
    month: "2026-11",
    months: 3,
    depth: 1,
    include_pending: false,
    series: [
      { month: "2026-09", amount: money("1500.00") },
      { month: "2026-10", amount: money("2000.00") },
      { month: "2026-11", amount: money("1800.00") },
    ],
    total: trendRow(null, null, { now: "1800.00", previous: "2000.00", yearAgo: "0.00" }, { mom: "-10.00", yoy: null }),
    categories: [
      trendRow(CAT_RENT, "Rent", { now: "1450.00", previous: "1450.00", yearAgo: "0.00" }, { mom: "0.00", yoy: null }),
      trendRow(CAT_FOOD, "Food", { now: "350.00", previous: "550.00", yearAgo: "0.00" }, { mom: "-36.36", yoy: null }),
    ],
    unconverted: [],
    ...patch,
  };
}
