// SPDX-License-Identifier: AGPL-3.0-or-later
import "./wealth.css";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate } from "../../lib/dates";
import { RANGE_LABELS } from "../investments/performance";
import { formatFraction, labelOf } from "../investments/format";
import { PlanningPage, QueryState } from "../planning/PlanningPage";
import {
  getHistory,
  getLatest,
  getProjection,
  NET_WORTH_RANGES,
  netWorthKeys,
  type Granularity,
  type NetWorthLatest,
  type NetWorthRange,
} from "./api";
import { accountRows, classRows, historyPoints, projectionPoints, type BreakdownRow } from "./chartData";
import { SeriesChart } from "./SeriesChart";

const breakdownColumns = (caption: string): TableColumn<BreakdownRow>[] => [
  { id: "label", header: caption, rowHeader: true, cell: (row) => row.label },
  { id: "amount", header: "Amount", align: "end", cell: (row) => <MoneyText amount={row.amount} currency={row.currency} tone="none" /> },
];

function ScopeNote({ scope }: { scope: "household" | "visible_accounts" }) {
  return scope === "household" ? null : (
    <p role="status">
      <Badge tone="info">Your accounts only</Badge> <span className="muted">These figures cover the accounts you can see, not the whole household.</span>
    </p>
  );
}

function Breakdown({ latest }: { latest: NetWorthLatest }) {
  const assets = classRows(latest.by_class.assets);
  const liabilities = classRows(latest.by_class.liabilities);
  return (
    <div className="planning-grid">
      <Card title="Assets by class" headingLevel={3}>
        <Table caption="Assets by class" captionHidden columns={breakdownColumns("Class")} rows={assets} rowKey={(row) => row.key} maxHeight="none" empty="No assets." />
      </Card>
      <Card title="Liabilities by class" headingLevel={3}>
        <Table caption="Liabilities by class" captionHidden columns={breakdownColumns("Class")} rows={liabilities} rowKey={(row) => row.key} maxHeight="none" empty="No liabilities." />
      </Card>
      <Card title="Assets by account" headingLevel={3}>
        <Table caption="Assets by account" captionHidden columns={breakdownColumns("Account")} rows={accountRows(latest, "asset")} rowKey={(row) => row.key} maxHeight="md" empty="No assets." />
      </Card>
      <Card title="Liabilities by account" headingLevel={3}>
        <Table caption="Liabilities by account" captionHidden columns={breakdownColumns("Account")} rows={accountRows(latest, "liability")} rowKey={(row) => row.key} maxHeight="md" empty="No liabilities." />
      </Card>
    </div>
  );
}

function ProjectionCard() {
  const [months, setMonths] = useState(12);
  const query = useQuery({ queryKey: netWorthKeys.projection(months), queryFn: () => getProjection(months) });
  const data = query.data;
  return (
    <Card
      title="Projection"
      headingLevel={2}
      actions={
        <Field label="Months ahead">
          <Select
            value={String(months)}
            onChange={(event) => { setMonths(Number(event.target.value)); }}
            options={[6, 12, 24, 60, 120].map((value) => ({ value: String(value), label: String(value) }))}
          />
        </Field>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="the projection">
        {data === undefined ? null : (
          <div className="planning-form">
            <SeriesChart
              points={projectionPoints(data)}
              series={[
                { key: "trend", label: "Trend", color: "var(--color-accent, #2457d6)" },
                { key: "savings", label: "From savings", color: "var(--color-success, #067647)" },
              ]}
              currency={data.currency}
              label="Net worth projection"
            />
            <dl className="wealth-figures">
              <div>
                <dt>Net worth now</dt>
                <dd><MoneyText amount={data.current.amount} currency={data.current.currency} tone="none" /></dd>
              </div>
              <div>
                <dt>Trend per month</dt>
                <dd>{data.trend === null ? "Not enough history" : <MoneyText amount={data.trend.slope_per_month.amount} currency={data.trend.slope_per_month.currency} tone="signed" signDisplay="exceptZero" />}</dd>
              </div>
              <div>
                <dt>Average monthly savings</dt>
                <dd><MoneyText amount={data.savings.average_monthly_savings.amount} currency={data.savings.average_monthly_savings.currency} tone="signed" signDisplay="exceptZero" /></dd>
              </div>
              <div>
                <dt>Savings rate</dt>
                <dd>{formatFraction(data.savings.savings_rate)}</dd>
              </div>
            </dl>
            <p className="muted">
              Projections extend past results in a straight line. They are an illustration, not a forecast. The trend is based on the last {String(data.basis_months)} months.
            </p>
          </div>
        )}
      </QueryState>
    </Card>
  );
}

export function NetWorthPage() {
  const [range, setRange] = useState<NetWorthRange>("1y");
  const [granularity, setGranularity] = useState<Granularity | "">("");
  const history = useQuery({ queryKey: netWorthKeys.history(range, granularity), queryFn: () => getHistory(range, granularity) });
  const latest = useQuery({ queryKey: netWorthKeys.latest, queryFn: getLatest });
  const hist = history.data;
  const now = latest.data;
  return (
    <PlanningPage title="Net worth">
      <QueryState loading={latest.isPending} error={latest.error} label="net worth">
        {now === undefined ? null : (
          <>
            <ScopeNote scope={now.scope} />
            <Card title={`As of ${formatDate(now.as_of) ?? now.as_of}`} headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Net worth</dt>
                  <dd><MoneyText amount={now.net.amount} currency={now.net.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Assets</dt>
                  <dd><MoneyText amount={now.assets.amount} currency={now.assets.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Liabilities</dt>
                  <dd><MoneyText amount={now.liabilities.amount} currency={now.liabilities.currency} tone="none" /></dd>
                </div>
                {hist?.change == null ? null : (
                  <div>
                    <dt>Change over {labelOf(RANGE_LABELS, range).toLowerCase()}</dt>
                    <dd><MoneyText amount={hist.change.amount} currency={hist.change.currency} tone="signed" signDisplay="exceptZero" /></dd>
                  </div>
                )}
              </dl>
              {now.unconverted.length > 0 || now.unpriced_security_ids.length > 0 ? (
                <p role="status" className="muted">
                  {now.unconverted.length > 0 ? `${String(now.unconverted.length)} account(s) have no exchange rate and are left out. ` : ""}
                  {now.unpriced_security_ids.length > 0 ? `${String(now.unpriced_security_ids.length)} security(ies) have no price and are left out.` : ""}
                </p>
              ) : null}
            </Card>
          </>
        )}
      </QueryState>

      <div className="planning-actions" role="group" aria-label="Range">
        {NET_WORTH_RANGES.map((value) => (
          <Button key={value} size="sm" variant={value === range ? "primary" : "secondary"} aria-pressed={value === range} onClick={() => { setRange(value); }}>
            {labelOf(RANGE_LABELS, value)}
          </Button>
        ))}
        <Field label="Detail">
          <Select
            value={granularity}
            placeholder="Automatic"
            onChange={(event) => { setGranularity(event.target.value as Granularity | ""); }}
            options={[{ value: "day", label: "Daily" }, { value: "week", label: "Weekly" }, { value: "month", label: "Monthly" }]}
          />
        </Field>
      </div>
      <QueryState loading={history.isPending} error={history.error} label="the history">
        {hist === undefined ? null : hist.points.length === 0 ? (
          <EmptyState title="No history yet" description="Add accounts and balances to see net worth over time." />
        ) : (
          <Card title="Over time" headingLevel={2}>
            <SeriesChart
              points={historyPoints(hist)}
              series={[
                { key: "net", label: "Net worth" },
                { key: "assets", label: "Assets", color: "var(--color-success, #067647)" },
                { key: "liabilities", label: "Liabilities", color: "var(--color-danger, #b42318)" },
              ]}
              currency={hist.currency}
              label="Net worth over time"
            />
          </Card>
        )}
      </QueryState>
      {now === undefined ? null : <Breakdown latest={now} />}
      <ProjectionCard />
    </PlanningPage>
  );
}
