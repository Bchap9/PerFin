// SPDX-License-Identifier: AGPL-3.0-or-later
/** Link a transaction to the other leg of a transfer, or unlink it. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts } from "../accounts/api";
import { linkTransfer, transferCandidates, txKeys, unlinkTransfer, type Transaction } from "./api";

export function TransferDialog({ transaction, onClose }: { transaction: Transaction; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const accounts = useAccounts();
  const linked = transaction.transfer !== null;
  const candidates = useQuery({
    queryKey: txKeys.candidates(transaction.id),
    queryFn: () => transferCandidates(transaction.id),
    enabled: !linked,
  });
  const [error, setError] = useState<string | null>(null);
  const done = (title: string): void => {
    void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
    void queryClient.invalidateQueries({ queryKey: queryRoots.reports });
    toast.notify({ title, tone: "success" });
    onClose();
  };
  const link = useMutation({
    mutationFn: (counterpartId: string) => linkTransfer(transaction.id, counterpartId),
    onSuccess: () => {
      done("Transfer linked");
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const unlink = useMutation({
    mutationFn: () => unlinkTransfer(transaction.id),
    onSuccess: () => {
      done("Transfer unlinked");
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const accountName = (id: string): string => {
    const account = accounts.data?.find((item) => item.id === id);
    return account === undefined ? "Another account" : accountLabel(account);
  };
  return (
    <Dialog open onClose={onClose} title={linked ? "Transfer" : "Link as transfer"} size="md">
      <div className="ledger-stack">
        {linked ? (
          <>
            <p>This transaction is one leg of a transfer. Unlinking keeps both transactions.</p>
            <div className="ledger-actions">
              <Button
                variant="danger"
                loading={unlink.isPending}
                onClick={() => {
                  unlink.mutate();
                }}
              >
                Unlink transfer
              </Button>
            </div>
          </>
        ) : (
          <>
            <p>Pick the matching transaction on the other account.</p>
            {candidates.isPending ? <Spinner label="Finding matches" /> : null}
            {candidates.isError ? (
              <p role="alert" className="ledger-error">
                {errorMessage(candidates.error)}
              </p>
            ) : null}
            {candidates.isSuccess && candidates.data.length === 0 ? <p className="ledger-muted">No possible matches found.</p> : null}
            <ul className="account-list" aria-label="Possible matches">
              {candidates.data?.map((candidate) => (
                <li key={candidate.id}>
                  <span>
                    {formatDate(candidate.posted_date) ?? candidate.posted_date}, {accountName(candidate.account_id)}
                    {candidate.raw_payee === null ? "" : `, ${candidate.raw_payee}`}{" "}
                    <MoneyText amount={candidate.amount.amount} currency={candidate.amount.currency} />
                  </span>
                  <Button
                    size="sm"
                    loading={link.isPending && link.variables === candidate.id}
                    onClick={() => {
                      link.mutate(candidate.id);
                    }}
                  >
                    Link
                  </Button>
                </li>
              ))}
            </ul>
          </>
        )}
        {error === null ? null : (
          <p role="alert" className="ledger-error">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
