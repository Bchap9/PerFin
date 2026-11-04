// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { MoneyText } from "../../components/MoneyText";
import { subtractAmounts } from "../../lib/money";
import { QueryState } from "../planning/PlanningPage";
import { SeriesChart } from "../networth/SeriesChart";
import { getPerformance, investmentKeys, PERFORMANCE_RANGES, type PerformanceRange } from "./api";
import { formatFraction, labelOf } from "./format";
import { GainText } from "./parts";
import { RANGE_LABELS, twrSeries, valueSeries } from "./performance";

export function PerformanceTab({ account }: { account: string }) {
  const [range, setRange] = useState<PerformanceRange>("1y");
  const query = useQuery({ queryKey: investmentKeys.performance(range, account), queryFn: () => getPerformance(range, account) });
  const data = query.data;
  return (
    <div className="planning-stack">
      <div className="planning-actions" role="group" aria-label="Range">
        {PERFORMANCE_RANGES.map((value) => (
          <Button key={value} size="sm" variant={value === range ? "primary" : "secondary"} aria-pressed={value === range} onClick={() => { setRange(value); }}>
            {labelOf(RANGE_LABELS, value)}
          </Button>
        ))}
      </div>
      <QueryState loading={query.isPending} error={query.error} label="performance">
        {data === undefined ? null : (
          <>
            {data.unpriced_security_ids.length > 0 || data.missing_rates.length > 0 ? (
              <p role="status" className="muted">
                Some securities have no price or exchange rate, so the figures below leave them out.
              </p>
            ) : null}
            <Card title="Returns" headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Time-weighted return</dt>
                  <dd>{formatFraction(data.twr)}</dd>
                </div>
                <div>
                  <dt>Time-weighted, annualised</dt>
                  <dd>{formatFraction(data.twr_annualized)}</dd>
                </div>
                <div>
                  <dt>Money-weighted return</dt>
                  <dd>{formatFraction(data.mwr)}</dd>
                </div>
                <div>
                  <dt>Money-weighted, annualised</dt>
                  <dd>{formatFraction(data.mwr_annualized)}</dd>
                </div>
              </dl>
              <p className="muted">Annualised figures need a period of a year or more.</p>
            </Card>
            <Card title="Value over time" headingLevel={2}>
              <SeriesChart
                points={valueSeries(data)}
                series={[{ key: "value", label: "Portfolio value" }]}
                currency={data.base_currency}
                label="Portfolio value"
              />
            </Card>
            <Card title="Cumulative return" headingLevel={2}>
              <SeriesChart points={twrSeries(data)} series={[{ key: "twr", label: "Return" }]} currency={data.base_currency} label="Cumulative time-weighted return" valueFormat="percent" />
            </Card>
            <Card title="The period" headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Start value</dt>
                  <dd><MoneyText amount={data.start_value.amount} currency={data.start_value.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>End value</dt>
                  <dd><MoneyText amount={data.end_value.amount} currency={data.end_value.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Money in</dt>
                  <dd><MoneyText amount={data.inflows.amount} currency={data.inflows.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Money out</dt>
                  <dd><MoneyText amount={data.outflows.amount} currency={data.outflows.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Income</dt>
                  <dd><MoneyText amount={data.income.amount} currency={data.income.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Fees</dt>
                  <dd><MoneyText amount={data.fees.amount} currency={data.fees.currency} tone="none" /></dd>
                </div>
              </dl>
            </Card>
            <Card title="Gains" headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Realised, short term</dt>
                  <dd><MoneyText amount={data.realized.short_term.amount} currency={data.realized.short_term.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Realised, long term</dt>
                  <dd><MoneyText amount={data.realized.long_term.amount} currency={data.realized.long_term.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Unrealised at the end</dt>
                  <dd><GainText gain={data.unrealized.total} cost={{ amount: costOf(data), currency: data.base_currency }} /></dd>
                </div>
              </dl>
            </Card>
          </>
        )}
      </QueryState>
    </div>
  );
}

/** Cost basis implied by end value and unrealised gain (end value minus gain). */
function costOf(data: { end_value: { amount: string }; unrealized: { total: { amount: string } } }): string {
  return subtractAmounts(data.end_value.amount, data.unrealized.total.amount);
}
