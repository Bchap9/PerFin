// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Delete account" for owners and admins (the roles `DELETE /accounts/{id}`
 * accepts): a dialog that says what goes with the account, offers closing
 * instead, and deletes only once the account's name has been typed.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { useAuthSession } from "../auth/session";
import { CloseAccountDialog } from "./AccountForms";
import {
  accountKeys,
  canDeleteAccount,
  deleteAccount,
  type Account,
} from "./api";

/** Everything that showed something of the deleted account. */
const AFFECTED = [
  queryRoots.accounts,
  queryRoots.transactions,
  queryRoots.recurring,
  queryRoots.investments,
  queryRoots.loans,
  queryRoots.assets,
  queryRoots.goals,
  queryRoots.budgets,
  queryRoots.reports,
  queryRoots.tax,
  queryRoots.documents,
  queryRoots.netWorth,
  queryRoots.dashboard,
] as const;

export function DeleteAccountDialog({
  account,
  onClose,
  onCloseInstead,
}: {
  account: Account;
  onClose: () => void;
  /** Present for an open account: swap this dialog for the close dialog. */
  onCloseInstead?: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const matches = typed.trim() === account.name.trim();
  const mutation = useMutation({
    mutationFn: () => deleteAccount(account.id),
    onSuccess: async () => {
      toast.notify({ title: `${account.name} deleted`, tone: "success" });
      // Leave first: refreshing the account's own page would only find it gone.
      await navigate({ to: "/accounts" });
      queryClient.removeQueries({ queryKey: accountKeys.one(account.id) });
      for (const queryKey of AFFECTED) {
        void queryClient.invalidateQueries({ queryKey });
      }
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!matches) {
      setError("Type the account name exactly as shown to delete it.");
      return;
    }
    setError(null);
    mutation.mutate();
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Delete ${account.name}?`}
      description="This permanently deletes the account and everything recorded on it. It cannot be undone."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            type="submit"
            form="delete-account-form"
            disabled={!matches}
            loading={mutation.isPending}
          >
            Delete account
          </Button>
        </>
      }
    >
      <form
        id="delete-account-form"
        className="ledger-stack"
        noValidate
        onSubmit={submit}
      >
        <div>
          <p className="delete-account__lead">Deleted with the account:</p>
          <ul className="delete-account__list">
            <li>every transaction on it, with its splits and tags</li>
            <li>its recorded balances and balance history</li>
            <li>scheduled transactions that post to it</li>
            <li>
              investment trades, lots and holdings, loan terms and payments,
              and asset valuations kept on it
            </li>
            <li>
              its statement import history and the access other members were
              given to it
            </li>
          </ul>
        </div>
        <p className="ledger-muted">
          Uploaded documents stay in Documents without an account, and the
          other side of a transfer stays in its own account.
        </p>
        {onCloseInstead === undefined ? (
          <p>
            This account is already closed: its history is kept and it no
            longer counts toward today&apos;s balances. Delete it only if the
            history should go too.
          </p>
        ) : (
          <p>
            Only want it out of the way? Close it instead: a closed account
            keeps its history, no longer counts toward today&apos;s balances
            and can be reopened.{" "}
            <Button size="sm" onClick={onCloseInstead}>
              Close it instead
            </Button>
          </p>
        )}
        <Field
          label="Type the account name to confirm"
          hint={
            <>
              The name is <strong>{account.name}</strong>.
            </>
          }
          error={error}
          required
        >
          <Input
            value={typed}
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            onChange={(event) => {
              setTyped(event.target.value);
              setError(null);
            }}
          />
        </Field>
      </form>
    </Dialog>
  );
}

/** The button and its dialogs; renders nothing for someone who may not delete. */
export function DeleteAccountButton({ account }: { account: Account }) {
  const { session } = useAuthSession();
  const [step, setStep] = useState<"idle" | "delete" | "close">("idle");
  if (!canDeleteAccount(session?.household?.role) || !account.can_edit) {
    return null;
  }
  const done = (): void => {
    setStep("idle");
  };
  return (
    <>
      <Button
        variant="danger"
        onClick={() => {
          setStep("delete");
        }}
      >
        Delete account
      </Button>
      {step === "delete" ? (
        <DeleteAccountDialog
          account={account}
          onClose={done}
          {...(account.is_closed
            ? {}
            : {
                onCloseInstead: () => {
                  setStep("close");
                },
              })}
        />
      ) : null}
      {step === "close" ? (
        <CloseAccountDialog open account={account} onClose={done} />
      ) : null}
    </>
  );
}
