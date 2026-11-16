// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A dated line chart (recharts) of one or more money series. Amounts are
 * decimal strings everywhere else; the chart alone needs numbers to place
 * points, so each point carries a display-only `plot` number per series and
 * every label is formatted from the original string.
 *
 * Colours come from the series tokens through the `chart-series-N` classes in
 * `app/styles/charts.css`, never from colours in code: the N-th series takes
 * class N (wrapping after six). The lines are stroked with `inherit`, so the
 * path takes the class colour from its group; Recharts would otherwise write
 * its own default colour onto the path. The legend is a plain list, which
 * also describes the chart to a screen reader.
 */
import "./wealth.css";

import { useId } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

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
  /**
   * Ignored, kept so older callers compile. A series is coloured by its
   * position through the `chart-series-N` classes; a colour in code would
   * bypass the theme and the CSP. Callers drop it as they are rebuilt.
   */
  color?: string;
}

const SERIES_CLASSES = ["chart-series-1", "chart-series-2", "chart-series-3", "chart-series-4", "chart-series-5", "chart-series-6"] as const;

/** The series class for the series at `index` (0-based), wrapping after six. */
export function seriesClass(index: number): string {
  return SERIES_CLASSES.at(index % SERIES_CLASSES.length) ?? SERIES_CLASSES[0];
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
  const legendId = useId();
  return (
    <div className="wealth-chart-block">
      <div className="wealth-chart" role="img" aria-label={`${label}, ${String(points.length)} points`} aria-describedby={legendId}>
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 256 }}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid className="chart-grid" stroke="currentColor" strokeDasharray="3 3" vertical={false} />
            <XAxis
              className="chart-axis"
              stroke="currentColor"
              tick={{ fill: "currentColor" }}
              dataKey="date"
              tickFormatter={(value: string) => formatDate(value, { style: "short" }) ?? value}
              minTickGap={32}
            />
            <YAxis
              className="chart-axis"
              stroke="currentColor"
              tick={{ fill: "currentColor" }}
              width={valueFormat === "money" ? 80 : 56}
              tickFormatter={(value: number) =>
                valueFormat === "money" ? (formatMoney(String(value), currency, { fractionDigits: 0 }) ?? String(value)) : `${String(Math.round(value * 100))}%`
              }
            />
            <Tooltip
              wrapperClassName="chart-tooltip"
              labelFormatter={(value) => (typeof value === "string" ? (formatDate(value) ?? value) : "")}
              formatter={(_value, name, item) => {
                const payload = item.payload as { exact: Record<string, string> };
                const key = typeof name === "string" ? name : "";
                const def = series.find((entry) => entry.key === key);
                return [fmt(new Map(Object.entries(payload.exact)).get(key) ?? ""), def?.label ?? key];
              }}
            />
            {series.map((entry, index) => (
              <Line
                key={entry.key}
                className={seriesClass(index)}
                type="monotone"
                dataKey={entry.key}
                name={entry.key}
                stroke="inherit"
                strokeWidth={2}
                dot={false}
                activeDot={{ className: `${seriesClass(index)} chart-active-dot`, stroke: "currentColor" }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <ul className="chart-legend" id={legendId} aria-label="Series">
        {series.map((entry, index) => (
          <li key={entry.key} className="chart-legend__item">
            <svg className="chart-legend__swatch" viewBox="0 0 16 4" width="16" height="4" aria-hidden="true" focusable="false">
              <rect className={seriesClass(index)} x="0" y="0" width="16" height="4" rx="2" />
            </svg>
            {entry.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
