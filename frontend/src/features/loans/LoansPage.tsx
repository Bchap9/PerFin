// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { formatFraction } from "../investments/format";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { createLoan, listLoans, loanKeys, type Loan } from "./api";
import { buildLoanCreate, emptyLoanForm, type LoanForm, type LoanProblems } from "./loanForm";
import { LoanTermsFields } from "./LoanTermsFields";

export const LOAN_ACCOUNT_TYPES = new Set(["loan", "mortgage", "line_of_credit"]);

function NewLoanDialog({ taken, onClose }: { taken: ReadonlySet<string>; onClose: () => void }) {
  const accounts = useAccounts();
  const candidates = (accounts.data ?? []).filter((a) => LOAN_ACCOUNT_TYPES.has(a.type) && !a.is_closed && a.can_edit && !taken.has(a.id));
  const [accountId, setAccountId] = useState("");
  const [form, setForm] = useState<LoanForm>(() => emptyLoanForm(todayIsoDate()));
  const [problems, setProblems] = useState<LoanProblems>({});
  const save = useAction(createLoan, { invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts], success: "Loan added" });
  const account = candidates.find((entry) => entry.id === (accountId === "" ? (candidates[0]?.id ?? "") : accountId));
  const currency = account?.currency ?? "USD";

  const submit = async (): Promise<void> => {
    const result = buildLoanCreate(form, account);
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
      title="New loan"
      description="Terms for a loan, mortgage or line of credit account."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Add loan
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Account" error={problems.account} required hint={candidates.length === 0 ? "Create a loan, mortgage or line of credit account first." : undefined}>
          <Select value={account?.id ?? ""} onChange={(event) => { setAccountId(event.target.value); }} options={candidates.map((entry) => ({ value: entry.id, label: entry.nickname ?? entry.name }))} />
        </Field>
        <LoanTermsFields
          form={form}
          problems={problems}
          currency={currency}
          onChange={(key, value) => {
            setForm((current) => ({ ...current, [key]: value }));
          }}
        />
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

export function LoansPage() {
  const query = useQuery({ queryKey: loanKeys.list, queryFn: listLoans });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const [adding, setAdding] = useState(false);
  const loans = query.data ?? [];

  const columns: TableColumn<Loan>[] = [
    {
      id: "name",
      header: "Loan",
      rowHeader: true,
      cell: (loan) => (
        <Link to="/loans/$loanId" params={{ loanId: loan.id }}>
          {accountName(loan.account_id)}
        </Link>
      ),
    },
    { id: "lender", header: "Lender", cell: (loan) => loan.lender ?? "" },
    { id: "balance", header: "Balance", align: "end", cell: (loan) => <MoneyText amount={loan.projection.balance.amount} currency={loan.projection.balance.currency} tone="none" /> },
    { id: "rate", header: "Rate", align: "end", cell: (loan) => formatFraction(loan.rate, 3) },
    { id: "payment", header: "Payment", align: "end", cell: (loan) => <MoneyText amount={loan.payment_amount.amount} currency={loan.payment_amount.currency} tone="none" /> },
    { id: "next", header: "Next due", cell: (loan) => (loan.projection.next_due_date === null ? "" : (formatDate(loan.projection.next_due_date) ?? loan.projection.next_due_date)) },
    { id: "payoff", header: "Payoff", cell: (loan) => (loan.projection.payoff_date === null ? "" : (formatDate(loan.projection.payoff_date) ?? loan.projection.payoff_date)) },
  ];

  return (
    <PlanningPage
      title="Loans"
      actions={
        <Button variant="primary" onClick={() => { setAdding(true); }}>
          New loan
        </Button>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="loans">
        {loans.length === 0 ? (
          <EmptyState title="No loans yet" description="Add the terms of a loan or mortgage to see its schedule and payoff date." />
        ) : (
          <Table caption="Loans" columns={columns} rows={loans} rowKey={(loan) => loan.id} maxHeight="none" />
        )}
      </QueryState>
      {adding ? <NewLoanDialog taken={new Set(loans.map((loan) => loan.account_id))} onClose={() => { setAdding(false); }} /> : null}
    </PlanningPage>
  );
}
