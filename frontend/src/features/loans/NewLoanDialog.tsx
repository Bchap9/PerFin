// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The terms of a loan for an account of a loan type that has none yet (made
 * without terms, or left by an import). A new loan account with its terms is
 * made in "Add account" (`AccountFormDialog`).
 */
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, type Account } from "../accounts/api";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { createLoan } from "./api";
import { buildLoanCreate, emptyLoanForm, type LoanForm, type LoanProblems } from "./loanForm";
import { LoanTermsFields } from "./LoanTermsFields";

/** The account types a loan may have (`LOAN_ACCOUNT_TYPES` on the server). */
export const LOAN_ACCOUNT_TYPES: ReadonlySet<string> = new Set(["loan", "mortgage", "line_of_credit"]);

export function NewLoanDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const [form, setForm] = useState<LoanForm>(() => emptyLoanForm(todayIsoDate()));
  const [problems, setProblems] = useState<LoanProblems>({});
  const save = useAction(createLoan, { invalidate: [queryRoots.loans, queryRoots.netWorth, queryRoots.accounts], success: "Loan added" });
  const name = accountLabel(account);

  const submit = async (): Promise<void> => {
    const result = buildLoanCreate(form, { id: account.id, currency: account.currency });
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
      title={`Loan terms for ${name}`}
      description="With its terms a loan gets a schedule, a payoff date and the extra payment simulator."
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
        <LoanTermsFields
          form={form}
          problems={problems}
          currency={account.currency}
          onChange={(key, value) => {
            setForm((current) => ({ ...current, [key]: value }));
          }}
        />
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}
