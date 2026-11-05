// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { addPayment, deletePayment, listPayments, loanKeys, type Loan, type LoanPayment } from "./api";

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

export function PaymentsTab({ loan }: { loan: Loan }) {
  const currency = loan.principal_original.currency;
  const query = useQuery({ queryKey: loanKeys.payments(loan.id), queryFn: () => listPayments(loan.id) });
  const [paidOn, setPaidOn] = useState(todayIsoDate());
  const [principal, setPrincipal] = useState("");
  const [interest, setInterest] = useState("");
  const [escrow, setEscrow] = useState("");
  const [fees, setFees] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts];
  const add = useAction((body: Parameters<typeof addPayment>[1]) => addPayment(loan.id, body), { invalidate, success: "Payment recorded" });
  const remove = useAction((id: string) => deletePayment(loan.id, id), { invalidate, success: "Payment deleted" });

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
            if (!isIsoDate(paidOn)) {
              setProblem("Enter a valid date.");
              return;
            }
            if (!isDecimalString(principal.trim()) || amountSign(principal.trim()) < 0 || !isDecimalString(interest.trim()) || amountSign(interest.trim()) < 0) {
              setProblem("Enter principal and interest of zero or more.");
              return;
            }
            if (!optionalAmount(escrow) || !optionalAmount(fees)) {
              setProblem("Escrow and fees must be zero or more.");
              return;
            }
            setProblem(null);
            void add
              .run({
                paid_on: paidOn,
                principal: { amount: principal.trim(), currency },
                interest: { amount: interest.trim(), currency },
                ...(escrow.trim() === "" ? {} : { escrow: { amount: escrow.trim(), currency } }),
                ...(fees.trim() === "" ? {} : { fees: { amount: fees.trim(), currency } }),
              })
              .then((done) => {
                if (done !== undefined) {
                  setPrincipal("");
                  setInterest("");
                  setEscrow("");
                  setFees("");
                }
              });
          }}
        >
          <Field label="Paid on">
            <Input type="date" value={paidOn} onChange={(event) => { setPaidOn(event.target.value); }} />
          </Field>
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
          <Button type="submit" variant="primary" loading={add.busy}>
            Record payment
          </Button>
        </form>
        <ErrorLine message={problem ?? add.error ?? remove.error} />
      </Card>
      <QueryState loading={query.isPending} error={query.error} label="payments">
        <Table caption="Payments" columns={columns} rows={query.data ?? []} rowKey={(p) => p.id} maxHeight="md" empty="No payments recorded." />
      </QueryState>
    </div>
  );
}
