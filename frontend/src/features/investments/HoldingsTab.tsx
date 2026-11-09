// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate } from "../../lib/dates";
import { nameLookup, useAccounts } from "../planning/queries";
import { QueryState } from "../planning/PlanningPage";
import { getHoldings, investmentKeys, type Holding, type Holdings, type Lot } from "./api";
import { formatFraction, formatQuantity, securityLabel } from "./format";
import { AllocationBars, GainText, titleCase } from "./parts";

function LotsDialog({ holding, onClose }: { holding: Holding; onClose: () => void }) {
  const columns: TableColumn<Lot>[] = [
    { id: "open", header: "Opened", rowHeader: true, cell: (lot) => formatDate(lot.open_date) ?? lot.open_date },
    { id: "qty", header: "Units left", align: "end", cell: (lot) => formatQuantity(lot.remaining_quantity) },
    { id: "unit", header: "Cost per unit", align: "end", cell: (lot) => <MoneyText amount={lot.cost_per_unit} currency={lot.cost_basis.currency} tone="none" fractionDigits={4} /> },
    { id: "cost", header: "Cost basis", align: "end", cell: (lot) => <MoneyText amount={lot.cost_basis.amount} currency={lot.cost_basis.currency} tone="none" /> },
    {
      id: "value",
      header: "Market value",
      align: "end",
      cell: (lot) => (lot.market_value === null ? "No price" : <MoneyText amount={lot.market_value.amount} currency={lot.market_value.currency} tone="none" />),
    },
    {
      id: "gain",
      header: "Unrealised",
      align: "end",
      cell: (lot) => (lot.unrealized_gain === null ? "n/a" : <GainText gain={lot.unrealized_gain} cost={lot.cost_basis} />),
    },
    { id: "term", header: "Term", cell: (lot) => <Badge tone={lot.term === "long" ? "info" : "neutral"}>{lot.term === "long" ? "Long term" : "Short term"}</Badge> },
  ];
  return (
    <Dialog open onClose={onClose} size="lg" title={`Lots of ${securityLabel(holding.security)}`} footer={<Button onClick={onClose}>Close</Button>}>
      <Table caption={`Lots of ${holding.security.name}`} captionHidden columns={columns} rows={holding.lots} rowKey={(lot) => lot.id} maxHeight="md" empty="No open lots." />
    </Dialog>
  );
}

function Totals({ data }: { data: Holdings }) {
  return (
    <Card title="Portfolio" headingLevel={2}>
      <dl className="wealth-figures">
        <div>
          <dt>Market value</dt>
          <dd>
            <MoneyText amount={data.market_value.amount} currency={data.market_value.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Cost basis</dt>
          <dd>
            <MoneyText amount={data.cost_basis.amount} currency={data.cost_basis.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Unrealised gain</dt>
          <dd>
            <GainText gain={data.unrealized.total} cost={data.cost_basis} />
          </dd>
        </div>
        <div>
          <dt>Short term</dt>
          <dd>
            <MoneyText amount={data.unrealized.short_term.amount} currency={data.unrealized.short_term.currency} tone="none" />
          </dd>
        </div>
        <div>
          <dt>Long term</dt>
          <dd>
            <MoneyText amount={data.unrealized.long_term.amount} currency={data.unrealized.long_term.currency} tone="none" />
          </dd>
        </div>
      </dl>
    </Card>
  );
}

export function HoldingsTab({ account }: { account: string }) {
  const query = useQuery({ queryKey: investmentKeys.holdings(account), queryFn: () => getHoldings(account) });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const [lotsOf, setLotsOf] = useState<Holding | null>(null);
  const data = query.data;

  const columns: TableColumn<Holding>[] = [
    {
      id: "security",
      header: "Security",
      rowHeader: true,
      cell: (h) => (
        <>
          {h.security.symbol === null ? (
            <strong>{h.security.name}</strong>
          ) : (
            <>
              <strong>{h.security.symbol}</strong> <span className="muted">{h.security.name}</span>
            </>
          )}
        </>
      ),
    },
    { id: "account", header: "Account", cell: (h) => accountName(h.account_id) },
    { id: "qty", header: "Quantity", align: "end", cell: (h) => formatQuantity(h.quantity) },
    {
      id: "price",
      header: "Price",
      align: "end",
      cell: (h) =>
        h.price === null ? (
          "No price"
        ) : (
          <>
            <MoneyText amount={h.price.close} currency={h.price.currency} tone="none" fractionDigits={4} />
            {h.price.basis === "trade" ? <span className="muted"> (last trade)</span> : null}
          </>
        ),
    },
    {
      id: "value",
      header: "Market value",
      align: "end",
      cell: (h) => (h.market_value === null ? "n/a" : <MoneyText amount={h.market_value.amount} currency={h.market_value.currency} tone="none" />),
    },
    { id: "cost", header: "Cost basis", align: "end", cell: (h) => <MoneyText amount={h.cost_basis.amount} currency={h.cost_basis.currency} tone="none" /> },
    {
      id: "gain",
      header: "Unrealised gain",
      align: "end",
      cell: (h) => (h.unrealized === null ? "n/a" : <GainText gain={h.unrealized.total} cost={h.cost_basis} />),
    },
    { id: "weight", header: "Weight", align: "end", cell: (h) => (h.weight === null ? "n/a" : formatFraction(h.weight, 1)) },
    {
      id: "lots",
      header: <span className="visually-hidden">Lots</span>,
      align: "end",
      cell: (h) => (
        <Button size="sm" aria-label={`Show lots of ${securityLabel(h.security)} in ${accountName(h.account_id)}`} onClick={() => { setLotsOf(h); }}>
          Lots
        </Button>
      ),
    },
  ];

  return (
    <QueryState loading={query.isPending} error={query.error} label="holdings">
      {data === undefined ? null : (
        <div className="planning-stack">
          {data.unpriced_security_ids.length > 0 || data.missing_rates.length > 0 ? (
            <p role="status" className="muted">
              {data.unpriced_security_ids.length > 0
                ? `${String(data.unpriced_security_ids.length)} security(ies) have no price and are left out of market value. Add a price under Prices.`
                : ""}{" "}
              {data.missing_rates.length > 0
                ? `Missing exchange rates for ${[...new Set(data.missing_rates.map((rate) => rate.currency))].join(", ")}.`
                : ""}
            </p>
          ) : null}
          {data.holdings.length === 0 ? (
            <EmptyState title="No holdings yet" description="Record a buy under Transactions, or import a brokerage statement." />
          ) : (
            <>
              <Totals data={data} />
              <Table caption="Holdings" columns={columns} rows={data.holdings} rowKey={(h) => `${h.account_id}:${h.security.id}`} maxHeight="none" />
              <div className="planning-grid">
                <Card title="By asset class" headingLevel={3}>
                  <AllocationBars title="Allocation by asset class" slices={data.allocation["asset_class"] ?? []} labels={titleCase} />
                </Card>
                <Card title="By sector" headingLevel={3}>
                  <AllocationBars title="Allocation by sector" slices={data.allocation["sector"] ?? []} labels={titleCase} />
                </Card>
                <Card title="By account" headingLevel={3}>
                  <AllocationBars title="Allocation by account" slices={data.allocation["account"] ?? []} labels={accountName} />
                </Card>
              </div>
            </>
          )}
          {lotsOf === null ? null : <LotsDialog holding={lotsOf} onClose={() => { setLotsOf(null); }} />}
        </div>
      )}
    </QueryState>
  );
}
