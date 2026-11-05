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
import { formatFraction, percentToFraction } from "../investments/format";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { createLoan, listLoans, loanKeys, type Loan } from "./api";

export const LOAN_ACCOUNT_TYPES = new Set(["loan", "mortgage", "line_of_credit"]);

function NewLoanDialog({ taken, onClose }: { taken: ReadonlySet<string>; onClose: () => void }) {
  const accounts = useAccounts();
  const candidates = (accounts.data ?? []).filter((a) => LOAN_ACCOUNT_TYPES.has(a.type) && !a.is_closed && a.can_edit && !taken.has(a.id));
  const [accountId, setAccountId] = useState("");
  const [principal, setPrincipal] = useState("");
  const [rate, setRate] = useState("");
  const [rateType, setRateType] = useState<"fixed" | "variable">("fixed");
  const [term, setTerm] = useState("");
  const [origination, setOrigination] = useState(todayIsoDate());
  const [payment, setPayment] = useState("");
  const [escrow, setEscrow] = useState("");
  const [lender, setLender] = useState("");
  const [problems, setProblems] = useState<Record<string, string>>({});
  const save = useAction(createLoan, { invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts], success: "Loan added" });
  const account = candidates.find((entry) => entry.id === (accountId === "" ? (candidates[0]?.id ?? "") : accountId));
  const currency = account?.currency ?? "USD";

  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    if (account === undefined) {
      found["account"] = "Choose a loan account.";
    }
    if (!isDecimalString(principal.trim()) || amountSign(principal.trim()) <= 0) {
      found["principal"] = "Enter the original amount, above zero.";
    }
    const fraction = percentToFraction(rate);
    if (fraction === null) {
      found["rate"] = "Enter the yearly rate as a percent, e.g. 6.125.";
    }
    if (!/^\d{1,3}$/.test(term.trim()) || Number(term) < 1) {
      found["term"] = "Enter the term in months.";
    }
    if (!isIsoDate(origination)) {
      found["origination"] = "Enter a valid date.";
    }
    if (payment.trim() !== "" && (!isDecimalString(payment.trim()) || amountSign(payment.trim()) <= 0)) {
      found["payment"] = "Enter a payment above zero, or leave it blank.";
    }
    if (escrow.trim() !== "" && (!isDecimalString(escrow.trim()) || amountSign(escrow.trim()) < 0)) {
      found["escrow"] = "Enter an escrow amount of zero or more.";
    }
    setProblems(found);
    if (Object.keys(found).length > 0 || account === undefined || fraction === null) {
      return;
    }
    const done = await save.run({
      account_id: account.id,
      principal_original: { amount: principal.trim(), currency },
      rate: fraction,
      rate_type: rateType,
      term_months: Number(term),
      origination_date: origination,
      ...(payment.trim() === "" ? {} : { payment_amount: { amount: payment.trim(), currency } }),
      ...(escrow.trim() === "" ? {} : { escrow_amount: { amount: escrow.trim(), currency } }),
      ...(lender.trim() === "" ? {} : { lender: lender.trim() }),
    });
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
        <Field label="Account" error={problems["account"]} required hint={candidates.length === 0 ? "Create a loan, mortgage or line of credit account first." : undefined}>
          <Select value={account?.id ?? ""} onChange={(event) => { setAccountId(event.target.value); }} options={candidates.map((entry) => ({ value: entry.id, label: entry.nickname ?? entry.name }))} />
        </Field>
        <Field label={`Original amount (${currency})`} error={problems["principal"]} required>
          <Input inputMode="decimal" value={principal} onChange={(event) => { setPrincipal(event.target.value); }} />
        </Field>
        <Field label="Yearly rate (%)" error={problems["rate"]} required>
          <Input inputMode="decimal" value={rate} onChange={(event) => { setRate(event.target.value); }} />
        </Field>
        <Field label="Rate type">
          <Select value={rateType} onChange={(event) => { setRateType(event.target.value === "variable" ? "variable" : "fixed"); }} options={[{ value: "fixed", label: "Fixed" }, { value: "variable", label: "Variable" }]} />
        </Field>
        <Field label="Term (months)" error={problems["term"]} required>
          <Input inputMode="numeric" value={term} onChange={(event) => { setTerm(event.target.value); }} />
        </Field>
        <Field label="Origination date" error={problems["origination"]} required>
          <Input type="date" value={origination} onChange={(event) => { setOrigination(event.target.value); }} />
        </Field>
        <Field label={`Monthly payment (${currency})`} error={problems["payment"]} hint="Principal and interest. Leave blank for the level payment.">
          <Input inputMode="decimal" value={payment} onChange={(event) => { setPayment(event.target.value); }} />
        </Field>
        <Field label={`Escrow per month (${currency})`} error={problems["escrow"]}>
          <Input inputMode="decimal" value={escrow} onChange={(event) => { setEscrow(event.target.value); }} />
        </Field>
        <Field label="Lender">
          <Input value={lender} onChange={(event) => { setLender(event.target.value); }} />
        </Field>
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
