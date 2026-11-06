// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts, type Account } from "../planning/queries";
import { useAction } from "../planning/useAction";
import {
  createInvestmentTxn,
  deleteInvestmentTxn,
  investmentKeys,
  listInvestmentTxns,
  listSecurities,
  type InvestmentTxn,
  type InvestmentTxnType,
  type Security,
} from "./api";
import { formatQuantity, labelOf, securityLabel, securityOptionLabel } from "./format";
import { SecurityDialog } from "./SecurityDialog";
import { buildTxnBody, emptyTxnForm, fieldsFor, TXN_TYPE_LABELS, type TxnForm, type TxnProblems } from "./txnForm";

export const INVESTMENT_ACCOUNT_TYPES = new Set(["brokerage", "retirement"]);

export function investmentAccounts(accounts: readonly Account[] | undefined): Account[] {
  return (accounts ?? []).filter((account) => INVESTMENT_ACCOUNT_TYPES.has(account.type) && !account.is_closed);
}

const TYPE_OPTIONS = (Object.keys(TXN_TYPE_LABELS) as InvestmentTxnType[]).map((value) => ({ value, label: labelOf(TXN_TYPE_LABELS, value) }));

export function TxnDialog({
  accounts,
  securities,
  defaultAccount,
  onClose,
}: {
  accounts: readonly Account[];
  securities: readonly Security[];
  defaultAccount: string;
  onClose: () => void;
}) {
  const first = accounts.find((account) => account.id === defaultAccount) ?? accounts[0];
  const [form, setForm] = useState<TxnForm>({ ...emptyTxnForm(todayIsoDate(), first?.currency ?? "USD"), accountId: first?.id ?? "" });
  const [problems, setProblems] = useState<TxnProblems>({});
  const [addingSecurity, setAddingSecurity] = useState(false);
  const save = useAction(createInvestmentTxn, { invalidate: [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard], success: "Transaction recorded" });
  const used = fieldsFor(form.type);
  const set = <K extends keyof TxnForm>(key: K, value: TxnForm[K]): void => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const submit = async (): Promise<void> => {
    const result = buildTxnBody(form);
    if (!result.ok) {
      setProblems(result.problems);
      return;
    }
    setProblems({});
    const done = await save.run(result.body);
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="New investment transaction"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Record transaction
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Account" error={problems.account} required>
          <Select
            value={form.accountId}
            placeholder="Choose an account"
            onChange={(event) => {
              const account = accounts.find((entry) => entry.id === event.target.value);
              setForm((current) => ({ ...current, accountId: event.target.value, currency: account?.currency ?? current.currency }));
            }}
            options={accounts.map((account) => ({ value: account.id, label: `${account.nickname ?? account.name} (${account.currency})` }))}
          />
        </Field>
        <Field label="Type">
          <Select value={form.type} onChange={(event) => { set("type", event.target.value as InvestmentTxnType); }} options={TYPE_OPTIONS} />
        </Field>
        {used.security ? (
          <div className="planning-row">
            <Field label="Security" error={problems.security}>
              <Select
                value={form.securityId}
                placeholder="Choose a security"
                onChange={(event) => { set("securityId", event.target.value); }}
                options={securities.map((security) => ({ value: security.id, label: securityOptionLabel(security) }))}
              />
            </Field>
            <Button onClick={() => { setAddingSecurity(true); }}>New security</Button>
          </div>
        ) : null}
        <Field label="Trade date" error={problems.date} required>
          <Input type="date" value={form.tradeDate} onChange={(event) => { set("tradeDate", event.target.value); }} />
        </Field>
        {used.quantity ? (
          <Field label="Quantity" error={problems.quantity} required>
            <Input inputMode="decimal" value={form.quantity} onChange={(event) => { set("quantity", event.target.value); }} />
          </Field>
        ) : null}
        {used.price ? (
          <Field label="Price per unit" error={problems.price} hint="In the security's currency.">
            <Input inputMode="decimal" value={form.price} onChange={(event) => { set("price", event.target.value); }} />
          </Field>
        ) : null}
        {used.fees ? (
          <Field label={`Fees (${form.currency})`} error={problems.fees}>
            <Input inputMode="decimal" value={form.fees} onChange={(event) => { set("fees", event.target.value); }} />
          </Field>
        ) : null}
        {used.amount ? (
          <Field
            label={`Amount (${form.currency})`}
            error={problems.amount}
            hint={
              form.type === "buy" || form.type === "sell" || form.type === "reinvest"
                ? "Leave blank to use quantity x price plus or minus fees. Enter it as a positive number."
                : form.type === "fee"
                  ? "Enter the fee as a positive number."
                  : "In the account's currency."
            }
          >
            <Input inputMode="decimal" value={form.amount} onChange={(event) => { set("amount", event.target.value); }} />
          </Field>
        ) : null}
        {used.ratio ? (
          <fieldset className="planning-row">
            <legend>Ratio</legend>
            <Field label="New units" error={problems.ratio}>
              <Input inputMode="decimal" value={form.ratioNew} onChange={(event) => { set("ratioNew", event.target.value); }} />
            </Field>
            <Field label="Old units">
              <Input inputMode="decimal" value={form.ratioOld} onChange={(event) => { set("ratioOld", event.target.value); }} />
            </Field>
          </fieldset>
        ) : null}
        <Field label="Memo">
          <Input value={form.memo} onChange={(event) => { set("memo", event.target.value); }} />
        </Field>
        <ErrorLine message={save.error} />
      </div>
      {addingSecurity ? (
        <SecurityDialog
          defaultCurrency={form.currency}
          onClose={() => { setAddingSecurity(false); }}
          onCreated={(security) => { set("securityId", security.id); }}
        />
      ) : null}
    </Dialog>
  );
}

export function TransactionsTab({ account }: { account: string }) {
  const [type, setType] = useState("");
  const [security, setSecurity] = useState("");
  const [adding, setAdding] = useState(false);
  const accounts = useAccounts();
  const securities = useQuery({ queryKey: investmentKeys.securities, queryFn: listSecurities });
  const query = useQuery({
    queryKey: investmentKeys.transactions(account, security, type),
    queryFn: () => listInvestmentTxns({ account, security, type }),
  });
  const accountName = nameLookup(accounts.data);
  const securityById = new Map((securities.data ?? []).map((entry) => [entry.id, entry]));
  const securityLabelOf = (id: string): string => {
    const found = securityById.get(id);
    return found === undefined ? "Unknown" : securityLabel(found);
  };
  const remove = useAction(deleteInvestmentTxn, { invalidate: [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard], success: "Transaction deleted" });
  const editable = investmentAccounts(accounts.data).filter((entry) => entry.can_edit);

  const columns: TableColumn<InvestmentTxn>[] = [
    { id: "date", header: "Date", rowHeader: true, cell: (t) => formatDate(t.trade_date) ?? t.trade_date },
    { id: "type", header: "Type", cell: (t) => labelOf(TXN_TYPE_LABELS, t.type) },
    { id: "account", header: "Account", cell: (t) => accountName(t.account_id) },
    { id: "security", header: "Security", cell: (t) => (t.security_id === null ? "" : securityLabelOf(t.security_id)) },
    { id: "qty", header: "Quantity", align: "end", cell: (t) => (t.quantity === null ? "" : formatQuantity(t.quantity)) },
    { id: "price", header: "Price", align: "end", cell: (t) => (t.price === null ? "" : formatQuantity(t.price)) },
    { id: "amount", header: "Amount", align: "end", cell: (t) => <MoneyText amount={t.amount.amount} currency={t.amount.currency} /> },
    { id: "fees", header: "Fees", align: "end", cell: (t) => <MoneyText amount={t.fees.amount} currency={t.fees.currency} tone="none" /> },
    {
      id: "remove",
      header: <span className="visually-hidden">Delete</span>,
      align: "end",
      cell: (t) => (
        <Button size="sm" variant="danger" aria-label={`Delete ${labelOf(TXN_TYPE_LABELS, t.type)} of ${t.trade_date}`} onClick={() => void remove.run(t.id)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="planning-stack">
      <div className="planning-row">
        <Field label="Type">
          <Select value={type} placeholder="All types" onChange={(event) => { setType(event.target.value); }} options={TYPE_OPTIONS} />
        </Field>
        <Field label="Security">
          <Select
            value={security}
            placeholder="All securities"
            onChange={(event) => { setSecurity(event.target.value); }}
            options={(securities.data ?? []).map((entry) => ({ value: entry.id, label: securityLabel(entry) }))}
          />
        </Field>
        <Button variant="primary" disabled={editable.length === 0} onClick={() => { setAdding(true); }}>
          New transaction
        </Button>
      </div>
      <ErrorLine message={remove.error} />
      <QueryState loading={query.isPending} error={query.error} label="transactions">
        <Table caption="Investment transactions" columns={columns} rows={query.data ?? []} rowKey={(t) => t.id} maxHeight="none" empty="No investment transactions." />
      </QueryState>
      {adding ? <TxnDialog accounts={editable} securities={securities.data ?? []} defaultAccount={account} onClose={() => { setAdding(false); }} /> : null}
    </div>
  );
}
