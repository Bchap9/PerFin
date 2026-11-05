// SPDX-License-Identifier: AGPL-3.0-or-later
import { toSeriesPoints, type SeriesPoint } from "../networth/SeriesChart";
import type { Performance, PerformanceRange } from "./api";

export const RANGE_LABELS: Record<PerformanceRange, string> = {
  "1m": "1 month",
  "3m": "3 months",
  "6m": "6 months",
  ytd: "Year to date",
  "1y": "1 year",
  "3y": "3 years",
  "5y": "5 years",
  max: "All time",
};

/** Time-weighted return per day as chart points (exact fractions; plot values are display-only). */
export function twrSeries(performance: Performance): SeriesPoint[] {
  return toSeriesPoints(performance.series.map((point) => ({ date: point.date, amounts: { twr: point.twr } })));
}

/** Portfolio value per day as chart points. */
export function valueSeries(performance: Performance): SeriesPoint[] {
  return toSeriesPoints(performance.series.map((point) => ({ date: point.date, amounts: { value: point.value.amount } })));
}
