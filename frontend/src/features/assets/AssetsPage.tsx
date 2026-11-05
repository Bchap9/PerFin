// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { labelOf } from "../investments/format";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { assetKeys, ASSET_KINDS, createAsset, KIND_LABELS, listAssets, METHODS, METHOD_LABELS, type Asset, type ValuationMethod } from "./api";

function NewAssetDialog({ taken, onClose }: { taken: ReadonlySet<string>; onClose: () => void }) {
  const accounts = useAccounts();
  const candidates = (accounts.data ?? []).filter((a) => a.type === "manual_asset" && !a.is_closed && a.can_edit && !taken.has(a.id));
  const [accountId, setAccountId] = useState("");
  const [method, setMethod] = useState<ValuationMethod>("manual");
  const [description, setDescription] = useState("");
  const [value, setValue] = useState("");
  const [valuedOn, setValuedOn] = useState(todayIsoDate());
  const [problems, setProblems] = useState<Record<string, string>>({});
  const save = useAction(createAsset, { invalidate: [queryRoots.assets, queryRoots.accounts, queryRoots.netWorth], success: "Asset added" });
  const account = candidates.find((entry) => entry.id === (accountId === "" ? (candidates[0]?.id ?? "") : accountId));

  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    if (account === undefined) {
      found["account"] = "Choose a manual asset account.";
    }
    if (value.trim() !== "" && (!isDecimalString(value.trim()) || amountSign(value.trim()) < 0)) {
      found["value"] = "Enter a value of zero or more.";
    }
    if (!isIsoDate(valuedOn)) {
      found["date"] = "Enter a valid date.";
    }
    setProblems(found);
    if (Object.keys(found).length > 0 || account === undefined) {
      return;
    }
    const done = await save.run({
      account_id: account.id,
      valuation_method: method,
      ...(description.trim() === "" ? {} : { description: description.trim() }),
      ...(value.trim() === "" ? {} : { value: { amount: value.trim(), currency: account.currency }, valued_on: valuedOn }),
    });
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="New asset"
      description="A property, vehicle or anything else you value yourself."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Add asset
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Account" error={problems["account"]} required hint={candidates.length === 0 ? "Create an account of type manual asset first." : undefined}>
          <Select value={account?.id ?? ""} onChange={(event) => { setAccountId(event.target.value); }} options={candidates.map((entry) => ({ value: entry.id, label: entry.nickname ?? entry.name }))} />
        </Field>
        <Field label="How you value it">
          <Select value={method} onChange={(event) => { setMethod(event.target.value as ValuationMethod); }} options={METHODS.map((entry) => ({ value: entry, label: labelOf(METHOD_LABELS, entry) }))} />
        </Field>
        <Field label="Description" hint="Optional. May hold an address; it stays out of the audit trail.">
          <Input value={description} onChange={(event) => { setDescription(event.target.value); }} />
        </Field>
        <Field label={`First value (${account?.currency ?? "USD"})`} error={problems["value"]} hint="Optional.">
          <Input inputMode="decimal" value={value} onChange={(event) => { setValue(event.target.value); }} />
        </Field>
        <Field label="Valued on" error={problems["date"]}>
          <Input type="date" value={valuedOn} onChange={(event) => { setValuedOn(event.target.value); }} />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

export function AssetsPage() {
  const query = useQuery({ queryKey: assetKeys.list, queryFn: listAssets });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const [adding, setAdding] = useState(false);
  const assets = query.data ?? [];

  const columns: TableColumn<Asset>[] = [
    {
      id: "name",
      header: "Asset",
      rowHeader: true,
      cell: (asset) => (
        <Link to="/assets/$assetId" params={{ assetId: asset.id }}>
          {asset.name}
        </Link>
      ),
    },
    { id: "account", header: "Account", cell: (asset) => accountName(asset.account_id) },
    { id: "kind", header: "Kind", cell: (asset) => labelOf(KIND_LABELS, asset.kind) },
    { id: "method", header: "Valued by", cell: (asset) => labelOf(METHOD_LABELS, asset.valuation_method) },
    {
      id: "value",
      header: "Current value",
      align: "end",
      cell: (asset) => (asset.current_value === null ? "No value yet" : <MoneyText amount={asset.current_value.value.amount} currency={asset.current_value.value.currency} tone="none" />),
    },
    { id: "on", header: "As of", cell: (asset) => (asset.current_value === null ? "" : (formatDate(asset.current_value.valued_on) ?? asset.current_value.valued_on)) },
  ];

  return (
    <PlanningPage
      title="Assets"
      actions={
        <Button variant="primary" onClick={() => { setAdding(true); }}>
          New asset
        </Button>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="assets">
        {assets.length === 0 ? (
          <EmptyState title="No manual assets yet" description={`Track a home, a car or other things you value yourself (${ASSET_KINDS.map((k) => labelOf(KIND_LABELS, k).toLowerCase()).join(", ")}).`} />
        ) : (
          <Table caption="Manual assets" columns={columns} rows={assets} rowKey={(asset) => asset.id} maxHeight="none" />
        )}
      </QueryState>
      {adding ? <NewAssetDialog taken={new Set(assets.map((asset) => asset.account_id))} onClose={() => { setAdding(false); }} /> : null}
    </PlanningPage>
  );
}
