// SPDX-License-Identifier: AGPL-3.0-or-later
/** "Delete tax year" for owners and admins (the roles `DELETE /tax-years/{year}` accepts), with a confirmation. */
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { deleteTaxYear, type TaxYear } from "./api";

function checklistText(year: TaxYear): string {
  const count = year.checklist.expected + year.checklist.received + year.checklist.not_applicable;
  if (count === 0) {
    return "its empty checklist";
  }
  return `its checklist of ${String(count)} ${count === 1 ? "document" : "documents"}`;
}

export function DeleteTaxYearButton({ year }: { year: TaxYear }) {
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const remove = useAction(
    async () => {
      await deleteTaxYear(year.year);
      // Leave before the tax queries refresh: this page would only find the year gone.
      await navigate({ to: "/tax" });
    },
    { invalidate: [queryRoots.tax], success: `Tax year ${String(year.year)} deleted` },
  );
  const close = (): void => {
    remove.clearError();
    setConfirming(false);
  };
  return (
    <>
      <Button
        variant="danger"
        onClick={() => {
          setConfirming(true);
        }}
      >
        Delete tax year
      </Button>
      {confirming ? (
        <Dialog
          open
          onClose={close}
          title={`Delete tax year ${String(year.year)}?`}
          description={`This deletes the year with its status, filing details and notes, and ${checklistText(year)}. It cannot be undone.`}
          footer={
            <>
              <Button onClick={close}>Cancel</Button>
              <Button variant="danger" loading={remove.busy} onClick={() => void remove.run()}>
                Delete tax year
              </Button>
            </>
          }
        >
          <div className="planning-form">
            <p>Uploaded documents, the amounts read from them and your transactions are kept.</p>
            <ErrorLine message={remove.error} />
          </div>
        </Dialog>
      ) : null}
    </>
  );
}
