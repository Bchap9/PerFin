// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Balance history line chart (recharts). Amounts stay decimal strings
 * everywhere else; the chart alone needs numbers to place points, so the
 * plotted value is a display-only conversion and every label is formatted
 * from the original string. Colours come from the chart classes
 * (`app/styles/charts.css`): the attributes only defer to them.
 */
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatDate } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import type { BalanceHistory } from "./api";

export interface ChartPoint {
  date: string;
  amount: string;
  /** Display-only. */
  plot: number;
}

export function chartPoints(history: BalanceHistory): ChartPoint[] {
  return history.items.map((point) => ({
    date: point.date,
    amount: point.balance.amount,
    plot: Number(point.balance.amount),
  }));
}

export function BalanceChart({ history }: { history: BalanceHistory }) {
  const points = chartPoints(history);
  const currency = history.currency;
  return (
    <div
      className="account-chart"
      role="img"
      aria-label={`Balance history, ${String(points.length)} points`}
    >
      <ResponsiveContainer
        width="100%"
        height="100%"
        initialDimension={{ width: 640, height: 256 }}
      >
        <LineChart
          data={points}
          margin={{ top: 8, right: 16, bottom: 8, left: 8 }}
        >
          <CartesianGrid
            className="chart-grid"
            stroke="currentColor"
            strokeDasharray="3 3"
            vertical={false}
          />
          <XAxis
            className="chart-axis"
            stroke="currentColor"
            tick={{ fill: "currentColor" }}
            dataKey="date"
            tickFormatter={(value: string) =>
              formatDate(value, { style: "short" }) ?? value
            }
            minTickGap={32}
          />
          <YAxis
            className="chart-axis"
            stroke="currentColor"
            tick={{ fill: "currentColor" }}
            width={80}
            tickFormatter={(value: number) =>
              formatMoney(String(value), currency, { fractionDigits: 0 }) ??
              String(value)
            }
          />
          <Tooltip
            wrapperClassName="chart-tooltip"
            labelFormatter={(label) =>
              typeof label === "string" ? (formatDate(label) ?? label) : ""
            }
            formatter={(_value, _name, item) => {
              const payload = item.payload as ChartPoint;
              return [
                formatMoney(payload.amount, currency) ?? payload.amount,
                "Balance",
              ];
            }}
          />
          <Line
            className="chart-series-1"
            type="monotone"
            dataKey="plot"
            stroke="currentColor"
            dot={false}
            activeDot={{ className: "chart-series-1 chart-active-dot", stroke: "currentColor" }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
