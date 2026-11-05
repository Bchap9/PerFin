// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Confirm a document deletion and choose what goes with it: the file only
 * (imported rows stay, unlinked; the same file can be uploaded again without
 * a duplicate hold) or the file and everything imported from it.
 */
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { deleteDocument, docKeys, getImported, type DocumentImported } from "./api";

type Mode = "file" | "data";

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

export function importedSummary(counts: DocumentImported): string[] {
  const parts: string[] = [];
  if (counts.transactions > 0) parts.push(plural(counts.transactions, "transaction", "transactions"));
  if (counts.investment_transactions > 0) {
    parts.push(plural(counts.investment_transactions, "investment transaction", "investment transactions"));
  }
  if (counts.loan_payments > 0) parts.push(plural(counts.loan_payments, "loan payment", "loan payments"));
  if (counts.balance_snapshots > 0) {
    parts.push(plural(counts.balance_snapshots, "balance snapshot", "balance snapshots"));
  }
  if (counts.child_documents > 0) {
    parts.push(plural(counts.child_documents, "attached document", "attached documents"));
  }
  return parts;
}

export function DeleteDocumentDialog({
  documentId,
  filename,
  onClose,
  onDeleted,
}: {
  documentId: string;
  filename: string;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const name = useId();
  const [mode, setMode] = useState<Mode>("file");
  const imported = useQuery({ queryKey: docKeys.imported(documentId), queryFn: () => getImported(documentId) });
  const summary = imported.data === undefined ? [] : importedSummary(imported.data);
  const remove = useAction((withData: boolean) => deleteDocument(documentId, withData), {
    invalidate: [
      queryRoots.documents,
      queryRoots.review,
      queryRoots.transactions,
      queryRoots.accounts,
      queryRoots.investments,
      queryRoots.loans,
      queryRoots.netWorth,
      queryRoots.reports,
      queryRoots.dashboard,
      queryRoots.tax,
    ],
    success: "Document deleted",
  });
  return (
    <Dialog
      open
      onClose={onClose}
      title="Delete document"
      description={`"${filename}" will be deleted permanently. Its encryption key is destroyed, so the file cannot be recovered.`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={remove.busy}
            disabled={imported.isPending}
            onClick={() => {
              void remove.run(mode === "data").then((done) => {
                if (done !== undefined) {
                  onDeleted?.();
                  onClose();
                }
              });
            }}
          >
            {mode === "data" ? "Delete file and data" : "Delete file"}
          </Button>
        </>
      }
    >
      {imported.isPending ? <Spinner label="Checking imported data" /> : null}
      {imported.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(imported.error)}
        </p>
      ) : null}
      {imported.data === undefined ? null : (
        <fieldset className="scope-grid">
          <legend>What to delete</legend>
          <label>
            <input
              type="radio"
              name={name}
              value="file"
              checked={mode === "file"}
              onChange={() => {
                setMode("file");
              }}
            />{" "}
            The file only
            <span className="ledger-muted">
              {" "}
              {summary.length === 0
                ? "(nothing was imported from it)"
                : "(imported data stays, unlinked from the file; the same file can be uploaded again)"}
            </span>
          </label>
          <label>
            <input
              type="radio"
              name={name}
              value="data"
              checked={mode === "data"}
              onChange={() => {
                setMode("data");
              }}
            />{" "}
            The file and everything imported from it
            <span className="ledger-muted"> ({summary.length === 0 ? "nothing else" : summary.join(", ")})</span>
          </label>
        </fieldset>
      )}
      <ErrorLine message={remove.error} />
    </Dialog>
  );
}
