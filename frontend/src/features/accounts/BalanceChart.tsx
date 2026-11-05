// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Balance history line chart (recharts). Amounts stay decimal strings
 * everywhere else; the chart alone needs numbers to place points, so the
 * plotted value is a display-only conversion and every label is formatted
 * from the original string.
 */
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

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
    <div className="account-chart" role="img" aria-label={`Balance history, ${String(points.length)} points`}>
      <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 256 }}>
        <LineChart data={points} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="date" tickFormatter={(value: string) => formatDate(value, { style: "short" }) ?? value} minTickGap={32} />
          <YAxis
            width={80}
            tickFormatter={(value: number) => formatMoney(String(value), currency, { fractionDigits: 0 }) ?? String(value)}
          />
          <Tooltip
            labelFormatter={(label) => (typeof label === "string" ? (formatDate(label) ?? label) : "")}
            formatter={(_value, _name, item) => {
              const payload = item.payload as ChartPoint;
              return [formatMoney(payload.amount, currency) ?? payload.amount, "Balance"];
            }}
          />
          <Line type="monotone" dataKey="plot" stroke="currentColor" dot={false} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
