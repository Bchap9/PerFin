// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, formatDecimal, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { labelOf } from "../investments/format";
import { SeriesChart } from "../networth/SeriesChart";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { useRouteParam } from "../planning/useRouteParam";
import {
  addValuation,
  assetKeys,
  ASSET_KINDS,
  deleteAsset,
  deleteValuation,
  getAsset,
  KIND_LABELS,
  listValuations,
  METHODS,
  METHOD_LABELS,
  updateAsset,
  type Asset,
  type AssetKind,
  type Valuation,
  type ValuationMethod,
} from "./api";
import { valuationPoints, valueChange } from "./valuations";

function EditDialog({ asset, onClose }: { asset: Asset; onClose: () => void }) {
  const [kind, setKind] = useState<AssetKind>(asset.kind);
  const [method, setMethod] = useState<ValuationMethod>(asset.valuation_method);
  const [description, setDescription] = useState(asset.description ?? "");
  const save = useAction(() => updateAsset(asset.id, { kind, valuation_method: method, description: description.trim() === "" ? null : description.trim() }), {
    invalidate: [queryRoots.assets, queryRoots.accounts],
    success: "Asset saved",
  });
  return (
    <Dialog
      open
      onClose={onClose}
      title="Edit asset"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={save.busy}
            onClick={() => {
              void save.run().then((done) => {
                if (done !== undefined) {
                  onClose();
                }
              });
            }}
          >
            Save
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Kind">
          <Select value={kind} onChange={(event) => { setKind(event.target.value as AssetKind); }} options={ASSET_KINDS.map((entry) => ({ value: entry, label: labelOf(KIND_LABELS, entry) }))} />
        </Field>
        <Field label="How you value it">
          <Select value={method} onChange={(event) => { setMethod(event.target.value as ValuationMethod); }} options={METHODS.map((entry) => ({ value: entry, label: labelOf(METHOD_LABELS, entry) }))} />
        </Field>
        <Field label="Description">
          <Input value={description} onChange={(event) => { setDescription(event.target.value); }} />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

function Valuations({ asset }: { asset: Asset }) {
  const accounts = useAccounts();
  const currency = accounts.data?.find((a) => a.id === asset.account_id)?.currency ?? asset.current_value?.value.currency ?? "USD";
  const query = useQuery({ queryKey: assetKeys.valuations(asset.id), queryFn: () => listValuations(asset.id) });
  const [valuedOn, setValuedOn] = useState(todayIsoDate());
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.assets, queryRoots.accounts, queryRoots.netWorth, queryRoots.dashboard];
  const add = useAction((body: Parameters<typeof addValuation>[1]) => addValuation(asset.id, body), { invalidate, success: "Value saved" });
  const remove = useAction((id: string) => deleteValuation(asset.id, id), { invalidate, success: "Value deleted" });
  const rows = query.data ?? [];
  const change = valueChange(rows);

  const columns: TableColumn<Valuation>[] = [
    { id: "date", header: "Date", rowHeader: true, cell: (v) => formatDate(v.valued_on) ?? v.valued_on },
    { id: "value", header: "Value", align: "end", cell: (v) => <MoneyText amount={v.value.amount} currency={v.value.currency} tone="none" /> },
    { id: "source", header: "Source", cell: (v) => (v.source === "manual" ? "Entered by you" : "Provider") },
    {
      id: "remove",
      header: <span className="visually-hidden">Delete</span>,
      align: "end",
      cell: (v) => (
        <Button size="sm" variant="danger" aria-label={`Delete the value of ${v.valued_on}`} onClick={() => void remove.run(v.id)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="planning-stack">
      <Card title="Add a value" headingLevel={3}>
        <form
          className="planning-row"
          aria-label="Add a value"
          onSubmit={(event) => {
            event.preventDefault();
            if (!isIsoDate(valuedOn)) {
              setProblem("Enter a valid date.");
              return;
            }
            if (!isDecimalString(value.trim()) || amountSign(value.trim()) < 0) {
              setProblem("Enter a value of zero or more.");
              return;
            }
            setProblem(null);
            void add.run({ valued_on: valuedOn, value: { amount: value.trim(), currency } }).then((done) => {
              if (done !== undefined) {
                setValue("");
              }
            });
          }}
        >
          <Field label="Date">
            <Input type="date" value={valuedOn} onChange={(event) => { setValuedOn(event.target.value); }} />
          </Field>
          <Field label={`Value (${currency})`}>
            <Input inputMode="decimal" value={value} onChange={(event) => { setValue(event.target.value); }} />
          </Field>
          <Button type="submit" variant="primary" loading={add.busy}>
            Save value
          </Button>
        </form>
        <ErrorLine message={problem ?? add.error ?? remove.error} />
      </Card>
      <QueryState loading={query.isPending} error={query.error} label="valuations">
        {rows.length >= 2 ? (
          <Card title="Value over time" headingLevel={3}>
            <SeriesChart points={valuationPoints(rows)} series={[{ key: "value", label: "Value" }]} currency={currency} label="Value over time" />
            {change === null ? null : (
              <p>
                Since the first value:{" "}
                <MoneyText amount={change.change} currency={currency} tone="signed" signDisplay="exceptZero" />
                {change.percent === null ? "" : ` (${formatDecimal(change.percent, { minimumFractionDigits: 2, signDisplay: "exceptZero" }) ?? change.percent}%)`}
              </p>
            )}
          </Card>
        ) : null}
        <Table caption="Valuation history" columns={columns} rows={rows} rowKey={(v) => v.id} maxHeight="md" empty="No values recorded." />
      </QueryState>
    </div>
  );
}

export function AssetDetailPage() {
  const assetId = useRouteParam("assetId");
  const navigate = useNavigate();
  const query = useQuery({ queryKey: assetKeys.one(assetId), queryFn: () => getAsset(assetId), enabled: assetId !== "" });
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const remove = useAction(() => deleteAsset(assetId), { invalidate: [queryRoots.assets, queryRoots.accounts, queryRoots.netWorth], success: "Asset deleted" });
  const asset = query.data;

  return (
    <PlanningPage
      title={asset?.name ?? "Asset"}
      actions={
        <>
          <Link to="/assets" className="btn btn--secondary btn--md">
            All assets
          </Link>
          {asset === undefined ? null : (
            <>
              <Button onClick={() => { setEditing(true); }}>Edit</Button>
              <Button variant="danger" onClick={() => { setConfirming(true); }}>
                Delete asset
              </Button>
            </>
          )}
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="the asset">
        {asset === undefined ? null : (
          <>
            <Card title="Summary" headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Current value</dt>
                  <dd>{asset.current_value === null ? "No value yet" : <MoneyText amount={asset.current_value.value.amount} currency={asset.current_value.value.currency} tone="none" />}</dd>
                </div>
                <div>
                  <dt>As of</dt>
                  <dd>{asset.current_value === null ? "" : (formatDate(asset.current_value.valued_on) ?? asset.current_value.valued_on)}</dd>
                </div>
                <div>
                  <dt>Kind</dt>
                  <dd>{labelOf(KIND_LABELS, asset.kind)}</dd>
                </div>
                <div>
                  <dt>Valued by</dt>
                  <dd>{labelOf(METHOD_LABELS, asset.valuation_method)}</dd>
                </div>
              </dl>
              {asset.description === null ? null : <p className="muted">{asset.description}</p>}
            </Card>
            <Valuations asset={asset} />
          </>
        )}
      </QueryState>
      {editing && asset !== undefined ? <EditDialog asset={asset} onClose={() => { setEditing(false); }} /> : null}
      {confirming ? (
        <Dialog
          open
          onClose={() => { setConfirming(false); }}
          title="Delete asset"
          description="This removes the asset details. The account and its values stay."
          footer={
            <>
              <Button onClick={() => { setConfirming(false); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run().then((done) => {
                    if (done !== undefined) {
                      void navigate({ to: "/assets" });
                    }
                  });
                }}
              >
                Delete asset
              </Button>
            </>
          }
        >
          <ErrorLine message={remove.error} />
        </Dialog>
      ) : null}
    </PlanningPage>
  );
}
