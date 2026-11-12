// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { addPayment, deletePayment, listPayments, loanKeys, type Loan, type LoanPayment, type LoanPaymentCreate } from "./api";

const money = (m: { amount: string; currency: string }) => <MoneyText amount={m.amount} currency={m.currency} tone="none" />;

function CheckBadge({ payment }: { payment: LoanPayment }) {
  const { status } = payment.schedule_check;
  if (status === "ok") {
    return <Badge tone="success">Matches schedule</Badge>;
  }
  if (status === "differs") {
    return <Badge tone="warning">Interest differs</Badge>;
  }
  return <Badge>Not checked</Badge>;
}

function optionalAmount(value: string): boolean {
  return value.trim() === "" || (isDecimalString(value.trim()) && amountSign(value.trim()) >= 0);
}

/** How a payment is entered: the amount paid alone, or its split as the lender's statement shows it. */
type Mode = "amount" | "split";

const MODE_OPTIONS: { value: Mode; label: string }[] = [
  { value: "amount", label: "Amount paid" },
  { value: "split", label: "Principal and interest" },
];

export function PaymentsTab({ loan }: { loan: Loan }) {
  const currency = loan.principal_original.currency;
  const query = useQuery({ queryKey: loanKeys.payments(loan.id), queryFn: () => listPayments(loan.id) });
  const [mode, setMode] = useState<Mode>("amount");
  const [paidOn, setPaidOn] = useState(todayIsoDate());
  const [amount, setAmount] = useState("");
  const [principal, setPrincipal] = useState("");
  const [interest, setInterest] = useState("");
  const [escrow, setEscrow] = useState("");
  const [fees, setFees] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts];
  const add = useAction((body: LoanPaymentCreate) => addPayment(loan.id, body), { invalidate, success: "Payment recorded" });
  const remove = useAction((id: string) => deletePayment(loan.id, id), { invalidate, success: "Payment deleted" });

  /** The request for what is typed, or the problem to show. */
  const build = (): LoanPaymentCreate | string => {
    if (!isIsoDate(paidOn)) {
      return "Enter a valid date.";
    }
    if (mode === "amount") {
      if (!isDecimalString(amount.trim()) || amountSign(amount.trim()) <= 0) {
        return "Enter the amount paid, above zero.";
      }
      return { paid_on: paidOn, amount: { amount: amount.trim(), currency } };
    }
    if (!isDecimalString(principal.trim()) || amountSign(principal.trim()) < 0 || !isDecimalString(interest.trim()) || amountSign(interest.trim()) < 0) {
      return "Enter principal and interest of zero or more.";
    }
    if (!optionalAmount(escrow) || !optionalAmount(fees)) {
      return "Escrow and fees must be zero or more.";
    }
    return {
      paid_on: paidOn,
      principal: { amount: principal.trim(), currency },
      interest: { amount: interest.trim(), currency },
      ...(escrow.trim() === "" ? {} : { escrow: { amount: escrow.trim(), currency } }),
      ...(fees.trim() === "" ? {} : { fees: { amount: fees.trim(), currency } }),
    };
  };

  const columns: TableColumn<LoanPayment>[] = [
    { id: "date", header: "Paid", rowHeader: true, cell: (p) => formatDate(p.paid_on) ?? p.paid_on },
    { id: "principal", header: "Principal", align: "end", cell: (p) => money(p.principal) },
    { id: "interest", header: "Interest", align: "end", cell: (p) => money(p.interest) },
    { id: "escrow", header: "Escrow", align: "end", cell: (p) => money(p.escrow) },
    { id: "fees", header: "Fees", align: "end", cell: (p) => money(p.fees) },
    { id: "total", header: "Total", align: "end", cell: (p) => money(p.total) },
    { id: "balance", header: "Balance after", align: "end", cell: (p) => (p.balance_after === null ? "" : money(p.balance_after)) },
    { id: "check", header: "Check", cell: (p) => <CheckBadge payment={p} /> },
    {
      id: "remove",
      header: <span className="visually-hidden">Delete</span>,
      align: "end",
      cell: (p) => (
        <Button size="sm" variant="danger" aria-label={`Delete the payment of ${p.paid_on}`} onClick={() => void remove.run(p.id)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="planning-stack">
      <Card title="Record a payment" headingLevel={3}>
        <form
          className="planning-row"
          aria-label="Record a payment"
          onSubmit={(event) => {
            event.preventDefault();
            const body = build();
            if (typeof body === "string") {
              setProblem(body);
              return;
            }
            setProblem(null);
            void add.run(body).then((done) => {
              if (done !== undefined) {
                setAmount("");
                setPrincipal("");
                setInterest("");
                setEscrow("");
                setFees("");
              }
            });
          }}
        >
          <Field label="Enter">
            <Select
              value={mode}
              onChange={(event) => {
                setMode(event.target.value === "split" ? "split" : "amount");
                setProblem(null);
              }}
              options={MODE_OPTIONS}
            />
          </Field>
          <Field label="Paid on">
            <Input type="date" value={paidOn} onChange={(event) => { setPaidOn(event.target.value); }} />
          </Field>
          {mode === "amount" ? (
            <Field label={`Amount paid (${currency})`}>
              <Input inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); }} />
            </Field>
          ) : (
            <>
              <Field label={`Principal (${currency})`}>
                <Input inputMode="decimal" value={principal} onChange={(event) => { setPrincipal(event.target.value); }} />
              </Field>
              <Field label="Interest">
                <Input inputMode="decimal" value={interest} onChange={(event) => { setInterest(event.target.value); }} />
              </Field>
              <Field label="Escrow">
                <Input inputMode="decimal" value={escrow} onChange={(event) => { setEscrow(event.target.value); }} />
              </Field>
              <Field label="Fees">
                <Input inputMode="decimal" value={fees} onChange={(event) => { setFees(event.target.value); }} />
              </Field>
            </>
          )}
          <Button type="submit" variant="primary" loading={add.busy}>
            Record payment
          </Button>
        </form>
        <p className="muted">
          {mode === "amount"
            ? "The whole payment in one figure. It is split the way a lender applies it: the interest due for the month first, then escrow if the loan has it, and the rest comes off the balance. Record payments oldest first."
            : "The split as your statement shows it."}
        </p>
        <ErrorLine message={problem ?? add.error ?? remove.error} />
      </Card>
      <QueryState loading={query.isPending} error={query.error} label="payments">
        <Table caption="Payments" columns={columns} rows={query.data ?? []} rowKey={(p) => p.id} maxHeight="md" empty="No payments recorded." />
      </QueryState>
    </div>
  );
}
