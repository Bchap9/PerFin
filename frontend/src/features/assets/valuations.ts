// SPDX-License-Identifier: AGPL-3.0-or-later
import { compareAmounts, subtractAmounts } from "../../lib/money";
import { ratioPercent } from "../investments/format";
import { toSeriesPoints, type SeriesPoint } from "../networth/SeriesChart";
import type { Valuation } from "./api";

/** Valuations oldest first as chart points (the API lists newest first). */
export function valuationPoints(valuations: readonly Valuation[]): SeriesPoint[] {
  return toSeriesPoints(
    [...valuations]
      .sort((a, b) => (a.valued_on < b.valued_on ? -1 : a.valued_on > b.valued_on ? 1 : 0))
      .map((entry) => ({ date: entry.valued_on, amounts: { value: entry.value.amount } })),
  );
}

export interface ValueChange {
  /** Latest minus earliest, as a decimal string. */
  change: string;
  /** Change as a percent of the earliest value; null when that is zero. */
  percent: string | null;
  direction: "up" | "down" | "flat";
}

/** Change from the earliest to the latest valuation; null with fewer than two. */
export function valueChange(valuations: readonly Valuation[]): ValueChange | null {
  if (valuations.length < 2) {
    return null;
  }
  const sorted = [...valuations].sort((a, b) => (a.valued_on < b.valued_on ? -1 : a.valued_on > b.valued_on ? 1 : 0));
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  if (first === undefined || last === undefined) {
    return null;
  }
  const change = subtractAmounts(last.value.amount, first.value.amount);
  const order = compareAmounts(change, "0");
  return { change, percent: ratioPercent(change, first.value.amount), direction: order > 0 ? "up" : order < 0 ? "down" : "flat" };
}
