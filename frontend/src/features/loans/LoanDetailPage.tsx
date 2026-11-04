// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { Tabs } from "../../components/Tabs";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { formatFraction } from "../investments/format";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { useRouteParam } from "../planning/useRouteParam";
import { deleteLoan, getLoan, getSchedule, loanKeys, type Loan, type ScheduleBasis } from "./api";
import { PaymentsTab } from "./PaymentsTab";
import { RateChangesTab } from "./RateChangesTab";
import { ScheduleTable } from "./ScheduleTable";
import { SimulatorTab } from "./SimulatorTab";

const dateText = (iso: string | null): string => (iso === null ? "None" : (formatDate(iso) ?? iso));

function ScheduleTab({ loan }: { loan: Loan }) {
  const [basis, setBasis] = useState<ScheduleBasis>("projected");
  const query = useQuery({ queryKey: loanKeys.schedule(loan.id, basis), queryFn: () => getSchedule(loan.id, basis) });
  const data = query.data;
  return (
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
  );
}

export function LoanDetailPage() {
  const loanId = useRouteParam("loanId");
  const navigate = useNavigate();
  const query = useQuery({ queryKey: loanKeys.one(loanId), queryFn: () => getLoan(loanId), enabled: loanId !== "" });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const [confirming, setConfirming] = useState(false);
  const remove = useAction(() => deleteLoan(loanId), { invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts], success: "Loan deleted" });
  const loan = query.data;

  return (
    <PlanningPage
      title={loan === undefined ? "Loan" : accountName(loan.account_id)}
      actions={
        <>
          <Link to="/loans" className="btn btn--secondary btn--md">
            All loans
          </Link>
          {loan === undefined ? null : (
            <Button variant="danger" onClick={() => { setConfirming(true); }}>
              Delete loan
            </Button>
          )}
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="the loan">
        {loan === undefined ? null : (
          <>
            <Card title="Summary" headingLevel={2}>
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
            </Card>
            <Tabs
              label="Loan"
              tabs={[
                { id: "schedule", label: "Schedule", content: <ScheduleTab loan={loan} /> },
                { id: "simulator", label: "Extra payments", content: <SimulatorTab loan={loan} /> },
                { id: "payments", label: "Payments", content: <PaymentsTab loan={loan} /> },
                { id: "rates", label: "Rate changes", content: <RateChangesTab loan={loan} /> },
              ]}
            />
          </>
        )}
      </QueryState>
      {confirming ? (
        <Dialog
          open
          onClose={() => { setConfirming(false); }}
          title="Delete loan"
          description="This removes the terms, payments and rate changes. The account and its balances stay."
          footer={
            <>
              <Button onClick={() => { setConfirming(false); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run().then((done) => {
                    if (done !== undefined) {
                      void navigate({ to: "/loans" });
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
    </PlanningPage>
  );
}
