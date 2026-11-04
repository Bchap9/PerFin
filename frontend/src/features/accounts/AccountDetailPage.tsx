// SPDX-License-Identifier: AGPL-3.0-or-later
import "./ledger.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Spinner } from "../../components/Spinner";
import { Table } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { addDays, formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { AccountFormDialog, CloseAccountDialog } from "./AccountForms";
import {
  accountKeys,
  accountLabel,
  addManualBalance,
  getAccount,
  getBalanceHistory,
  reopenAccount,
  useCanWrite,
  type Account,
} from "./api";
import { BalanceChart } from "./BalanceChart";

function ManualBalanceForm({ account }: { account: Account }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [date, setDate] = useState(todayIsoDate());
  const [amount, setAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () => addManualBalance(account.id, date, amount.trim(), account.currency),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      toast.notify({ title: "Balance recorded", tone: "success" });
      setAmount("");
      setError(null);
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!isDecimalString(amount.trim())) {
      setError("Enter the balance as a number such as 1250.00.");
      return;
    }
    if (!isIsoDate(date)) {
      setError("Enter a date.");
      return;
    }
    setError(null);
    mutation.mutate();
  };
  return (
    <form className="ledger-toolbar" aria-label="Record a balance" noValidate onSubmit={submit}>
      <Field label="As of">
        <Input
          type="date"
          value={date}
          onChange={(event) => {
            setDate(event.target.value);
          }}
        />
      </Field>
      <Field label={`Balance (${account.currency})`} error={error}>
        <Input
          value={amount}
          inputMode="decimal"
          onChange={(event) => {
            setAmount(event.target.value);
          }}
        />
      </Field>
      <Button type="submit" variant="primary" loading={mutation.isPending}>
        Record balance
      </Button>
    </form>
  );
}

export function AccountDetailPage() {
  const accountId = useParams({ strict: false, select: (params: Record<string, string | undefined>) => params["accountId"] ?? "" });
  const canWrite = useCanWrite();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [closing, setClosing] = useState(false);
  const account = useQuery({ queryKey: accountKeys.one(accountId), queryFn: () => getAccount(accountId) });
  const start = addDays(todayIsoDate(), -365);
  const history = useQuery({
    queryKey: accountKeys.balances(accountId, start, null),
    queryFn: () => getBalanceHistory(accountId, start, null),
    enabled: account.isSuccess,
  });
  const reopen = useMutation({
    mutationFn: () => reopenAccount(accountId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      toast.notify({ title: "Account reopened", tone: "success" });
    },
    onError: (caught) => {
      toast.notify({ title: "Could not reopen the account", description: errorMessage(caught), tone: "error" });
    },
  });

  if (account.isPending) {
    return <Spinner label="Loading account" />;
  }
  if (account.isError) {
    return (
      <section className="ledger-stack">
        <h1>Account not found</h1>
        <p role="alert" className="ledger-error">
          {errorMessage(account.error)}
        </p>
        <Link to="/accounts">Back to accounts</Link>
      </section>
    );
  }
  const data = account.data;
  const writable = canWrite && data.can_edit;
  const snapshots = [...(history.data?.snapshots ?? [])].sort((a, b) => b.as_of_date.localeCompare(a.as_of_date));

  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <div>
          <p>
            <Link to="/accounts">All accounts</Link>
          </p>
          <h1 id="page-title">{accountLabel(data)}</h1>
          <p className="ledger-muted">
            <MoneyText amount={data.balance.amount} currency={data.balance.currency} /> as of {formatDate(data.balance_date) ?? data.balance_date}
            {data.is_closed ? <Badge tone="neutral">Closed</Badge> : null}
          </p>
        </div>
        <div className="ledger-actions">
          <Link to="/transactions" search={{ account: data.id }} className="btn btn--secondary btn--md">
            View transactions
          </Link>
          {writable ? (
            <>
              <Button
                onClick={() => {
                  setEditing(true);
                }}
              >
                Edit
              </Button>
              {data.is_closed ? (
                <Button
                  loading={reopen.isPending}
                  onClick={() => {
                    reopen.mutate();
                  }}
                >
                  Reopen
                </Button>
              ) : (
                <Button
                  variant="danger"
                  onClick={() => {
                    setClosing(true);
                  }}
                >
                  Close account
                </Button>
              )}
            </>
          ) : null}
        </div>
      </div>
      <Card title="Balance history">
        {history.isPending ? <Spinner label="Loading balance history" /> : null}
        {history.isError ? (
          <p role="alert" className="ledger-error">
            {errorMessage(history.error)}
          </p>
        ) : null}
        {history.isSuccess ? (
          history.data.items.length === 0 ? (
            <p className="ledger-muted">No balance history in the last year.</p>
          ) : (
            <BalanceChart history={history.data} />
          )
        ) : null}
      </Card>
      <Card title="Recorded balances">
        {writable ? <ManualBalanceForm account={data} /> : null}
        <Table
          caption="Recorded balances"
          captionHidden
          maxHeight="sm"
          rows={snapshots}
          rowKey={(row) => row.id}
          empty="No recorded balances."
          columns={[
            { id: "date", header: "As of", cell: (row) => formatDate(row.as_of_date) ?? row.as_of_date, rowHeader: true },
            { id: "source", header: "Source", cell: (row) => row.source },
            {
              id: "balance",
              header: "Balance",
              align: "end",
              cell: (row) => <MoneyText amount={row.balance.amount} currency={row.balance.currency} />,
            },
          ]}
        />
      </Card>
      {editing ? (
        <AccountFormDialog
          open
          account={data}
          onClose={() => {
            setEditing(false);
          }}
        />
      ) : null}
      {closing ? (
        <CloseAccountDialog
          open
          account={data}
          onClose={() => {
            setClosing(false);
          }}
        />
      ) : null}
    </section>
  );
}
