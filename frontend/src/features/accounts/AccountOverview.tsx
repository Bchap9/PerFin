// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Overview tab of the account hub: the balance over the last year as a
 * chart, the recorded balances, and (for someone who may edit the account)
 * a form to record a balance by hand.
 */
import "./ledger.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

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
import { accountKeys, accountLabel, addManualBalance, getBalanceHistory, type Account } from "./api";
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

export function AccountOverview({ account, writable }: { account: Account; writable: boolean }) {
  const start = addDays(todayIsoDate(), -365);
  const history = useQuery({
    queryKey: accountKeys.balances(account.id, start, null),
    queryFn: () => getBalanceHistory(account.id, start, null),
  });
  const snapshots = [...(history.data?.snapshots ?? [])].sort((a, b) => b.as_of_date.localeCompare(a.as_of_date));
  return (
    <div className="account-hub__panel">
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
        {writable ? <ManualBalanceForm account={account} /> : null}
        <Table
          caption={`Recorded balances of ${accountLabel(account)}`}
          captionHidden
          maxHeight="sm"
          rows={snapshots}
          rowKey={(row) => row.id}
          empty="No recorded balances."
          columns={[
            {
              id: "date",
              header: "As of",
              cell: (row) => formatDate(row.as_of_date) ?? row.as_of_date,
              rowHeader: true,
            },
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
    </div>
  );
}
