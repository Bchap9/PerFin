// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A dated line chart (recharts) of one or more money series. Amounts are
 * decimal strings everywhere else; the chart alone needs numbers to place
 * points, so each point carries a display-only `plot` number per series and
 * every label is formatted from the original string.
 */
import "./wealth.css";

import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { formatDate } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import { formatFraction } from "../investments/format";

export interface SeriesPoint {
  date: string;
  /** Exact amounts by series key. */
  amounts: Readonly<Record<string, string>>;
  /** Display-only numbers by series key. */
  plot: Readonly<Record<string, number>>;
}

export interface SeriesDef {
  key: string;
  label: string;
  /** CSS colour; defaults to the text colour. */
  color?: string;
}

/** Build chart points from rows of exact amounts. */
export function toSeriesPoints(rows: readonly { date: string; amounts: Record<string, string> }[]): SeriesPoint[] {
  return rows.map((row) => ({
    date: row.date,
    amounts: row.amounts,
    plot: Object.fromEntries(Object.entries(row.amounts).map(([key, amount]) => [key, Number(amount)])),
  }));
}

export function SeriesChart({
  points,
  series,
  currency,
  label,
  valueFormat = "money",
}: {
  points: readonly SeriesPoint[];
  series: readonly SeriesDef[];
  currency: string;
  label: string;
  /** `money` formats amounts as currency; `percent` treats them as fractions. */
  valueFormat?: "money" | "percent";
}) {
  const fmt = (amount: string): string => {
    if (valueFormat === "money") {
      return formatMoney(amount, currency) ?? amount;
    }
    return formatFraction(amount);
  };
  const data = points.map((point) => ({ date: point.date, ...point.plot, exact: point.amounts }));
  return (
    <div className="wealth-chart" role="img" aria-label={`${label}, ${String(points.length)} points`}>
      <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 256 }}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="date" tickFormatter={(value: string) => formatDate(value, { style: "short" }) ?? value} minTickGap={32} />
          <YAxis
            width={valueFormat === "money" ? 80 : 56}
            tickFormatter={(value: number) =>
              valueFormat === "money" ? (formatMoney(String(value), currency, { fractionDigits: 0 }) ?? String(value)) : `${String(Math.round(value * 100))}%`
            }
          />
          <Tooltip
            labelFormatter={(value) => (typeof value === "string" ? (formatDate(value) ?? value) : "")}
            formatter={(_value, name, item) => {
              const payload = item.payload as { exact: Record<string, string> };
              const key = typeof name === "string" ? name : "";
              const def = series.find((entry) => entry.key === key);
              return [fmt(new Map(Object.entries(payload.exact)).get(key) ?? ""), def?.label ?? key];
            }}
          />
          {series.length > 1 ? <Legend formatter={(value: string) => series.find((entry) => entry.key === value)?.label ?? value} /> : null}
          {series.map((entry) => (
            <Line
              key={entry.key}
              type="monotone"
              dataKey={entry.key}
              name={entry.key}
              stroke={entry.color ?? "currentColor"}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
