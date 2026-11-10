// SPDX-License-Identifier: AGPL-3.0-or-later
/** "Edit loan": correct the terms of a loan with the fields it was created with. */
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { updateLoan, type Loan, type LoanUpdate } from "./api";
import { amortisationChanged, buildLoanUpdate, loanFormOf, type LoanForm, type LoanProblems } from "./loanForm";
import { LoanTermsFields } from "./LoanTermsFields";

export function EditLoanDialog({ loan, accountName, onClose }: { loan: Loan; accountName: string; onClose: () => void }) {
  const [form, setForm] = useState<LoanForm>(() => loanFormOf(loan));
  const [problems, setProblems] = useState<LoanProblems>({});
  const save = useAction((body: LoanUpdate) => updateLoan(loan.id, body), {
    invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts, queryRoots.dashboard],
    success: "Loan updated",
  });
  const currency = loan.principal_original.currency;
  // The stored payment does not follow the other terms by itself: say so while it is left as it was.
  const paymentKept = amortisationChanged(form, loan) && form.payment.trim() === loan.payment_amount.amount;

  const submit = async (): Promise<void> => {
    const result = buildLoanUpdate(form, loan);
    if (!result.ok) {
      setProblems(result.problems);
      return;
    }
    setProblems({});
    if (Object.keys(result.body).length === 0) {
      onClose(); // nothing changed, nothing to send
      return;
    }
    const done = await save.run(result.body);
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Edit loan"
      description="Correct the terms. The schedule, the payoff date and a balance that follows the schedule are worked out again."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save changes
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <p className="muted">
          The loan takes its name from its account, {accountName}. To rename it,{" "}
          <Link to="/accounts/$accountId" params={{ accountId: loan.account_id }}>
            edit the account
          </Link>
          .
        </p>
        <LoanTermsFields
          form={form}
          problems={problems}
          currency={currency}
          showPaymentDay
          paymentHint={
            paymentKept ? (
              <>
                The payment stays at <MoneyText amount={loan.payment_amount.amount} currency={currency} tone="none" /> unless you change it. Clear the field to use
                the level payment for the new terms.
              </>
            ) : (
              "Principal and interest. Clear the field to use the level payment for these terms."
            )
          }
          onChange={(key, value) => {
            setForm((current) => ({ ...current, [key]: value }));
          }}
        />
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}
