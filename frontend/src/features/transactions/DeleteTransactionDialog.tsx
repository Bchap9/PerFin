// SPDX-License-Identifier: AGPL-3.0-or-later
/** Confirm deleting one transaction; the dialog names the row so the right one goes. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { deleteTransaction, invalidateLedger, type Transaction } from "./api";

export interface DeleteTransactionDialogProps {
  transaction: Transaction;
  /** The row's payee or description, as the grid shows it. */
  label: string;
  /** The account's name, when it is known. */
  accountName?: string | undefined;
  onClose: () => void;
  /** Called once the transaction is gone (before `onClose`). */
  onDeleted?: (id: string) => void;
}

export function DeleteTransactionDialog({ transaction, label, accountName, onClose, onDeleted }: DeleteTransactionDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: () => deleteTransaction(transaction.id),
    onSuccess: () => {
      invalidateLedger(queryClient);
      toast.notify({ title: "Transaction deleted", tone: "success" });
      onDeleted?.(transaction.id);
      onClose();
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  return (
    <Dialog
      open
      onClose={onClose}
      size="sm"
      title="Delete this transaction?"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={remove.isPending}
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete transaction
          </Button>
        </>
      }
    >
      <div className="ledger-stack">
        <p className="tx-summary">
          <strong>{label}</strong>
          <span>
            {formatDate(transaction.posted_date) ?? transaction.posted_date}
            {accountName === undefined ? "" : `, ${accountName}`}
          </span>
          <MoneyText amount={transaction.amount.amount} currency={transaction.amount.currency} tone="signed" />
        </p>
        <p className="ledger-muted">
          It is removed from balances and reports. This cannot be undone.
          {transaction.transfer === null ? "" : " It is one leg of a transfer: the other transaction stays and is no longer marked as a transfer."}
          {transaction.splits.length === 0 ? "" : " Its split lines are deleted with it."}
        </p>
        {error === null ? null : (
          <p role="alert" className="ledger-error">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
