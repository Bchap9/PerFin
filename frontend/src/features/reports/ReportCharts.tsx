// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The report charts (recharts). Amounts are decimal strings everywhere else;
 * a chart alone needs numbers to place its marks, so every datum carries a
 * display-only `plot` number and every label is formatted from the exact
 * string. Each chart is one labelled image; the table next to it holds the
 * same figures for people who cannot see or hover the chart.
 *
 * Colours come from classes, never from colours in code (CSP style-src
 * 'self'): the series through `chart-series-N` (app/styles/charts.css, one
 * palette for both themes), grid and axes through `chart-grid` and
 * `chart-axis`, and the muted months and the net line through reports.css.
 * Every mark gets `currentColor` or `inherit` as its attribute so the class
 * decides. Series are told apart by more than hue (bars against a line, a
 * legend with the names), and text never takes a series colour.
 */
import "./reports.css";

import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Line,
  Rectangle,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type BarShapeProps,
} from "recharts";

import { formatMoney } from "../../lib/money";
import { monthTick, valueAxis, type BarDatum, type CashflowDatum, type MonthDatum, type ValueAxis } from "./chartData";

/** Marks take their colour from their class; the attribute only defers to it. */
const FROM_CLASS = "currentColor";
const AXIS = { className: "chart-axis", stroke: FROM_CLASS, tick: { fill: FROM_CLASS } } as const;
const GRID = { className: "chart-grid", stroke: FROM_CLASS } as const;
const ZERO_LINE = { className: "report-zero-line", stroke: FROM_CLASS } as const;

const COLUMN_SIZE = { width: 640, height: 288 };
const ROW_HEIGHT = 32;
const ROW_AXIS_HEIGHT = 44;
const NAME_LENGTH = 18;
const MONEY_WIDTH = 76;

export type KeyKind = "series-1" | "series-2" | "muted" | "net";

/** The names of the series, with a mark like the one in the chart. Shown for two or more series. */
export function ChartLegend({ items }: { items: readonly { kind: KeyKind; label: string }[] }) {
  return (
    <ul className="report-legend" aria-label="Chart key">
      {items.map((item) => (
        <li key={item.label}>
          <span className={`report-key report-key--${item.kind}`} aria-hidden="true" />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function Tip({ title, lines }: { title: string; lines: readonly { label: string; value: string; kind?: KeyKind }[] }) {
  return (
    <div className="report-tip">
      <p className="report-tip__title">{title}</p>
      {lines.map((line) => (
        <p key={line.label} className="report-tip__line">
          {line.kind === undefined ? null : <span className={`report-key report-key--${line.kind}`} aria-hidden="true" />}
          <strong>{line.value}</strong>
          <span className="report-tip__label">{line.label}</span>
        </p>
      ))}
    </div>
  );
}

function clip(text: string): string {
  return text.length > NAME_LENGTH ? `${text.slice(0, NAME_LENGTH - 1).trimEnd()}…` : text;
}

/** Exact amounts for tooltips; axis labels without decimals when every tick is a whole number. */
function moneyFormats(currency: string, axis: ValueAxis): { exact: (amount: string) => string; axis: (value: number) => string } {
  const digits = axis.whole ? { fractionDigits: 0 } : {};
  return {
    exact: (amount) => formatMoney(amount, currency) ?? amount,
    axis: (value) => formatMoney(String(value), currency, digits) ?? String(value),
  };
}

function axisDomain(axis: ValueAxis): [number, number] {
  return [axis.ticks[0] ?? 0, axis.ticks[axis.ticks.length - 1] ?? 1];
}

function Frame({ label, size, children }: { label: string; size: "columns" | "rows"; children: ReactNode }) {
  return (
    <div className={`report-chart report-chart--${size}`} role="img" aria-label={label}>
      {children}
    </div>
  );
}

/** Horizontal bars, largest first: spending or income by category, payee or account. */
export function RankedBarChart({ bars, currency, label, valueLabel }: { bars: readonly BarDatum[]; currency: string; label: string; valueLabel: string }) {
  const axis = valueAxis(bars.map((bar) => bar.plot));
  const money = moneyFormats(currency, axis);
  const height = bars.length * ROW_HEIGHT + ROW_AXIS_HEIGHT;
  return (
    <Frame label={`${label}, ${String(bars.length)} ${bars.length === 1 ? "bar" : "bars"}`} size="rows">
      <ResponsiveContainer width="100%" height={height} initialDimension={{ width: COLUMN_SIZE.width, height }}>
        <BarChart data={[...bars]} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 4 }}>
          {/* A line at every round value, also where a narrow screen has no room for the label. */}
          <CartesianGrid
            {...GRID}
            horizontal={false}
            verticalCoordinatesGenerator={({ offset }) => {
              const [low, high] = axisDomain(axis);
              return axis.ticks.map((tick) => offset.left + ((tick - low) / (high - low)) * offset.width);
            }}
          />
          <XAxis {...AXIS} type="number" domain={axisDomain(axis)} ticks={axis.ticks} tickFormatter={money.axis} axisLine={false} tickLine={false} interval="equidistantPreserveStart" />
          <YAxis {...AXIS} type="category" dataKey="label" width="auto" tickFormatter={clip} tickLine={false} interval={0} />
          <Tooltip
            content={({ active, payload }) => {
              const datum = payload[0]?.payload as BarDatum | undefined;
              return active && datum !== undefined ? <Tip title={datum.label} lines={[{ label: valueLabel, value: money.exact(datum.amount) }]} /> : null;
            }}
          />
          <Bar className="chart-series-1" dataKey="plot" fill={FROM_CLASS} radius={[0, 4, 4, 0]} maxBarSize={20} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
    </Frame>
  );
}

const ROLE_NOTES: ReadonlyMap<MonthDatum["role"], string> = new Map([
  ["current", "The month compared"],
  ["previous", "The month before"],
  ["year-ago", "A year earlier"],
]);

/**
 * One column per month. With `compare`, the three months a trend report
 * compares keep the series colour and the others step back to grey.
 */
export function MonthColumnChart({
  bars,
  currency,
  label,
  valueLabel,
  compare = false,
}: {
  bars: readonly MonthDatum[];
  currency: string;
  label: string;
  valueLabel: string;
  compare?: boolean;
}) {
  const axis = valueAxis(bars.map((bar) => bar.plot));
  const money = moneyFormats(currency, axis);
  const first = bars[0]?.month ?? "";
  return (
    <Frame label={`${label}, ${String(bars.length)} ${bars.length === 1 ? "month" : "months"}`} size="columns">
      <ResponsiveContainer width="100%" height="100%" initialDimension={COLUMN_SIZE}>
        <BarChart data={[...bars]} margin={{ top: 8, right: 16, bottom: 4, left: 4 }}>
          <CartesianGrid {...GRID} vertical={false} horizontalValues={axis.ticks} />
          <XAxis {...AXIS} dataKey="month" tickFormatter={(month: string) => monthTick(month, first)} tickLine={false} minTickGap={12} interval="equidistantPreserveStart" />
          <YAxis {...AXIS} width={MONEY_WIDTH} domain={axisDomain(axis)} ticks={axis.ticks} tickFormatter={money.axis} axisLine={false} tickLine={false} />
          <ReferenceLine {...ZERO_LINE} y={0} />
          <Tooltip
            content={({ active, payload }) => {
              const datum = payload[0]?.payload as MonthDatum | undefined;
              if (!active || datum === undefined) {
                return null;
              }
              const note = compare ? ROLE_NOTES.get(datum.role) : undefined;
              return <Tip title={note === undefined ? datum.label : `${datum.label} · ${note}`} lines={[{ label: valueLabel, value: money.exact(datum.amount) }]} />;
            }}
          />
          <Bar
            className="chart-series-1"
            dataKey="plot"
            fill={FROM_CLASS}
            radius={[4, 4, 0, 0]}
            maxBarSize={24}
            isAnimationActive={false}
            {...(compare
              ? {
                  shape: (props: BarShapeProps) => {
                    const datum = props.payload as MonthDatum;
                    // The datum's fields arrive among the props; its `role` ("current", "other") is not an ARIA role.
                    return <Rectangle {...props} role={undefined} fill={FROM_CLASS} className={datum.role === "other" ? "report-bar--muted" : "chart-series-1"} />;
                  },
                }
              : {})}
          />
        </BarChart>
      </ResponsiveContainer>
    </Frame>
  );
}

/** Money in and money out side by side per month, with net as a line on the same scale. */
export function CashflowChart({ bars, currency, label }: { bars: readonly CashflowDatum[]; currency: string; label: string }) {
  const axis = valueAxis(bars.flatMap((bar) => [bar.plotIn, bar.plotOut, bar.plotNet]));
  const money = moneyFormats(currency, axis);
  const first = bars[0]?.month ?? "";
  return (
    <Frame label={`${label}, ${String(bars.length)} ${bars.length === 1 ? "month" : "months"}`} size="columns">
      <ResponsiveContainer width="100%" height="100%" initialDimension={COLUMN_SIZE}>
        <ComposedChart data={[...bars]} margin={{ top: 8, right: 16, bottom: 4, left: 4 }} barGap={2} barCategoryGap="24%">
          <CartesianGrid {...GRID} vertical={false} horizontalValues={axis.ticks} />
          <XAxis {...AXIS} dataKey="month" tickFormatter={(month: string) => monthTick(month, first)} tickLine={false} minTickGap={12} interval="equidistantPreserveStart" />
          <YAxis {...AXIS} width={MONEY_WIDTH} domain={axisDomain(axis)} ticks={axis.ticks} tickFormatter={money.axis} axisLine={false} tickLine={false} />
          <ReferenceLine {...ZERO_LINE} y={0} />
          <Tooltip
            content={({ active, payload }) => {
              const datum = payload[0]?.payload as CashflowDatum | undefined;
              return active && datum !== undefined ? (
                <Tip
                  title={datum.label}
                  lines={[
                    { label: "Money in", value: money.exact(datum.inflow), kind: "series-1" },
                    { label: "Money out", value: money.exact(datum.outflow), kind: "series-2" },
                    { label: "Net", value: formatMoney(datum.net, currency, { signDisplay: "exceptZero" }) ?? datum.net, kind: "net" },
                  ]}
                />
              ) : null;
            }}
          />
          <Bar className="chart-series-1" dataKey="plotIn" fill={FROM_CLASS} radius={[4, 4, 0, 0]} maxBarSize={20} isAnimationActive={false} />
          <Bar className="chart-series-2" dataKey="plotOut" fill={FROM_CLASS} radius={[4, 4, 0, 0]} maxBarSize={20} isAnimationActive={false} />
          <Line
            className="report-net-line"
            type="linear"
            dataKey="plotNet"
            stroke={FROM_CLASS}
            strokeWidth={2}
            dot={{ className: "report-net-dot", r: 5, fill: FROM_CLASS, stroke: FROM_CLASS, strokeWidth: 2 }}
            activeDot={{ className: "report-net-dot", r: 6, fill: FROM_CLASS, stroke: FROM_CLASS, strokeWidth: 2 }}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </Frame>
  );
}
