// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A loan's four tabs on its account's hub (`/accounts/<account>?tab=…`):
 * Schedule (the terms, Edit loan and Delete loan, and the amortisation
 * schedule), Payments, Rate changes and Payoff (the extra payment
 * simulator). Each takes the loan's id and reads the loan itself, so a
 * change made in one tab shows in the others.
 *
 * Deleting the loan removes its terms, payments and rate changes; the
 * account and its balances stay, and the hub returns to its overview.
 */
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { formatFraction } from "../investments/format";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { deleteLoan, getLoan, getSchedule, loanKeys, type Loan, type ScheduleBasis } from "./api";
import { EditLoanDialog } from "./EditLoanDialog";
import { PaymentsTab } from "./PaymentsTab";
import { RateChangesTab } from "./RateChangesTab";
import { ScheduleTable } from "./ScheduleTable";
import { SimulatorTab } from "./SimulatorTab";

const dateText = (iso: string | null): string => (iso === null ? "None" : (formatDate(iso) ?? iso));

function useLoan(loanId: string) {
  return useQuery({ queryKey: loanKeys.one(loanId), queryFn: () => getLoan(loanId), enabled: loanId !== "" });
}

/** One tab's content: the loan once loaded, under a heading named after the tab. */
function LoanPanel({ loanId, title, children }: { loanId: string; title: string; children: (loan: Loan) => ReactNode }) {
  const query = useLoan(loanId);
  return (
    <div className="planning-stack">
      <h2 className="visually-hidden">{title}</h2>
      <QueryState loading={query.isPending} error={query.error} label="the loan">
        {query.data === undefined ? null : children(query.data)}
      </QueryState>
    </div>
  );
}

function Summary({ loan }: { loan: Loan }) {
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(false);
  const remove = useAction(() => deleteLoan(loan.id), {
    invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts],
    success: "Loan deleted",
  });
  // Terms may be changed or removed by someone who can edit the loan's account; the server checks again.
  const editable = accounts.data?.find((entry) => entry.id === loan.account_id)?.can_edit === true;
  return (
    <Card
      title="Loan terms"
      actions={
        editable ? (
          <>
            <Button size="sm" onClick={() => { setEditing(true); }}>
              Edit loan
            </Button>
            <Button size="sm" variant="danger" onClick={() => { setConfirming(true); }}>
              Delete loan
            </Button>
          </>
        ) : undefined
      }
    >
      <dl className="wealth-figures">
        <div>
          <dt>Balance owed</dt>
          <dd><MoneyText amount={loan.projection.balance.amount} currency={loan.projection.balance.currency} tone="none" /></dd>
        </div>
        <div>
          <dt>Rate</dt>
          <dd>{formatFraction(loan.rate, 3)} {loan.rate_type}</dd>
        </div>
        <div>
          <dt>Payment</dt>
          <dd><MoneyText amount={loan.payment_amount.amount} currency={loan.payment_amount.currency} tone="none" /></dd>
        </div>
        {loan.escrow_amount === null ? null : (
          <div>
            <dt>Escrow</dt>
            <dd><MoneyText amount={loan.escrow_amount.amount} currency={loan.escrow_amount.currency} tone="none" /></dd>
          </div>
        )}
        <div>
          <dt>Next due</dt>
          <dd>{dateText(loan.projection.next_due_date)}</dd>
        </div>
        <div>
          <dt>Payoff date</dt>
          <dd>{dateText(loan.projection.payoff_date)}</dd>
        </div>
        <div>
          <dt>Payments left</dt>
          <dd>{String(loan.projection.remaining_payments)}</dd>
        </div>
        <div>
          <dt>Interest still to pay</dt>
          <dd><MoneyText amount={loan.projection.remaining_interest.amount} currency={loan.projection.remaining_interest.currency} tone="none" /></dd>
        </div>
      </dl>
      <p className="muted">
        {loan.projection.balance_source === "scheduled"
          ? "No payments or statements are recorded yet, so the balance assumes every payment due so far was made."
          : "The balance comes from recorded payments and statements."}{" "}
        Original amount{" "}
        <MoneyText amount={loan.principal_original.amount} currency={loan.principal_original.currency} tone="none" />, {String(loan.term_months)} months from{" "}
        {dateText(loan.origination_date)}, maturing {dateText(loan.maturity_date)}.{loan.lender === null ? "" : ` Lender: ${loan.lender}.`}
      </p>
      {editing ? <EditLoanDialog loan={loan} accountName={accountName(loan.account_id)} onClose={() => { setEditing(false); }} /> : null}
      {confirming ? (
        <Dialog
          open
          onClose={() => { setConfirming(false); }}
          title="Delete loan"
          description="This removes the terms, payments and rate changes. The account stays. Its balances and transactions are kept."
          footer={
            <>
              <Button onClick={() => { setConfirming(false); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run().then((done) => {
                    if (done !== undefined) {
                      setConfirming(false);
                      void navigate({ to: "/accounts/$accountId", params: { accountId: loan.account_id } });
                    }
                  });
                }}
              >
                Delete loan
              </Button>
            </>
          }
        >
          <ErrorLine message={remove.error} />
        </Dialog>
      ) : null}
    </Card>
  );
}

function Schedule({ loan }: { loan: Loan }) {
  const [basis, setBasis] = useState<ScheduleBasis>("projected");
  const query = useQuery({ queryKey: loanKeys.schedule(loan.id, basis), queryFn: () => getSchedule(loan.id, basis) });
  const data = query.data;
  return (
    <Card title="Amortisation schedule">
      <div className="planning-stack">
        <div className="planning-actions" role="group" aria-label="Schedule basis">
          <Button size="sm" variant={basis === "projected" ? "primary" : "secondary"} aria-pressed={basis === "projected"} onClick={() => { setBasis("projected"); }}>
            From today
          </Button>
          <Button size="sm" variant={basis === "original" ? "primary" : "secondary"} aria-pressed={basis === "original"} onClick={() => { setBasis("original"); }}>
            As originated
          </Button>
        </div>
        <QueryState loading={query.isPending} error={query.error} label="schedule">
          {data === undefined ? null : (
            <>
              <dl className="wealth-figures">
                <div>
                  <dt>Payments</dt>
                  <dd>{String(data.totals.payments)}</dd>
                </div>
                <div>
                  <dt>Total interest</dt>
                  <dd><MoneyText amount={data.totals.interest.amount} currency={data.totals.interest.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Total paid</dt>
                  <dd><MoneyText amount={data.totals.paid.amount} currency={data.totals.paid.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Payoff</dt>
                  <dd>{dateText(data.totals.payoff_date)}</dd>
                </div>
              </dl>
              <ScheduleTable rows={data.rows} caption={`Amortisation schedule, ${basis === "original" ? "as originated" : "from today"}`} />
            </>
          )}
        </QueryState>
      </div>
    </Card>
  );
}

/** Schedule: the loan's terms with Edit loan and Delete loan, then its amortisation schedule. */
export function LoanSchedule({ loanId }: { loanId: string }) {
  const query = useLoan(loanId);
  return (
    <div className="planning-stack">
      <QueryState loading={query.isPending} error={query.error} label="the loan">
        {query.data === undefined ? null : (
          <>
            <Summary loan={query.data} />
            <Schedule loan={query.data} />
          </>
        )}
      </QueryState>
    </div>
  );
}

/** Payments: the history, and to those who may edit the account, the form to record one and Delete. */
export function LoanPayments({ loanId, editable }: { loanId: string; editable: boolean }) {
  return (
    <LoanPanel loanId={loanId} title="Payments">
      {(loan) => <PaymentsTab loan={loan} editable={editable} />}
    </LoanPanel>
  );
}

/** Rate changes: the history, and to those who may edit the account, the form to add one and Delete. */
export function LoanRates({ loanId, editable }: { loanId: string; editable: boolean }) {
  return (
    <LoanPanel loanId={loanId} title="Rate changes">
      {(loan) => <RateChangesTab loan={loan} editable={editable} />}
    </LoanPanel>
  );
}

/** Payoff: what extra payments would save (the simulator). */
export function LoanPayoff({ loanId }: { loanId: string }) {
  return (
    <LoanPanel loanId={loanId} title="Payoff with extra payments">
      {(loan) => <SimulatorTab loan={loan} />}
    </LoanPanel>
  );
}
