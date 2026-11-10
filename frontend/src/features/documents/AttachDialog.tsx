// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Attach a document to the transaction or account it belongs with (`POST
 * /documents/{id}/link`), and show what it is attached to.
 *
 * A link only records that the file and its target belong together: nothing
 * is imported or changed, and it is shown with the document. PerFin makes
 * the same link itself when a receipt matches one transaction with
 * certainty. The caller must be able to change the document and the target
 * (the server answers `404` for a target they cannot edit), attaching the
 * same pair twice is harmless, and there is no way to remove a link yet.
 */
import { useInfiniteQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage, isApiError } from "../../lib/api/client";
import { compareIsoDates, formatDate, formatDateTime } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { useAccounts, usePayees, type Account, type Payee } from "../planning/queries";
import { useIdempotencyKey } from "../../lib/idempotency";
import {
  docKeys,
  findTransactions,
  getLinkedTransaction,
  linkDocument,
  type AttachTarget,
  type DocumentDetail,
  type DocumentLink,
  type LedgerTransaction,
  type TransactionSearch,
} from "./api";

function accountName(account: Pick<Account, "name" | "nickname">): string {
  return account.nickname !== null && account.nickname !== "" ? account.nickname : account.name;
}

function day(iso: string): string {
  return formatDate(iso) ?? iso;
}

function payeeName(transaction: LedgerTransaction, payees: readonly Payee[] | undefined): string {
  return payees?.find((payee) => payee.id === transaction.payee_id)?.name ?? transaction.raw_payee ?? "No payee";
}

function amountText(transaction: LedgerTransaction): string {
  return formatMoney(transaction.amount.amount, transaction.amount.currency) ?? `${transaction.amount.amount} ${transaction.amount.currency}`;
}

/** "Oct 20, 2026, Cafe Uno, -$4.50": enough to tell one transaction from the next. */
export function transactionLabel(transaction: LedgerTransaction, payees: readonly Payee[] | undefined): string {
  return `${day(transaction.posted_date)}, ${payeeName(transaction, payees)}, ${amountText(transaction)}`;
}

/** Why the server would not attach, in words a person can act on. */
export function attachProblem(error: unknown): string {
  if (isApiError(error) && error.status === 404) {
    if (error.slug === "transaction-not-found") {
      return "That transaction no longer exists, or it is in an account you may not change.";
    }
    if (error.slug === "account-not-found") {
      return "That account no longer exists, or you may not change it.";
    }
  }
  return errorMessage(error);
}

function isLinked(links: readonly DocumentLink[], type: AttachTarget, id: string): boolean {
  return links.some((link) => link.target_type === type && link.target_id === id);
}

function TransactionLine({
  transaction,
  payees,
  accounts,
}: {
  transaction: LedgerTransaction;
  payees: readonly Payee[] | undefined;
  accounts: readonly Account[] | undefined;
}) {
  const account = accounts?.find((item) => item.id === transaction.account_id);
  return (
    <span>
      {day(transaction.posted_date)}, {payeeName(transaction, payees)}
      {account === undefined ? "" : `, ${accountName(account)}`}{" "}
      <MoneyText amount={transaction.amount.amount} currency={transaction.amount.currency} />
    </span>
  );
}

const LINK_KIND: Record<DocumentLink["target_type"], string> = {
  transaction: "Transaction",
  account: "Account",
  investment_txn: "Investment transaction",
  loan_payment: "Loan payment",
  tax_year: "Tax year",
};

/** What a document is attached to, with the control to attach it to something more. */
export function DocumentLinksCard({ document }: { document: DocumentDetail }) {
  const [attaching, setAttaching] = useState(false);
  const accounts = useAccounts();
  const payees = usePayees();
  const links = document.links;
  const transactionIds = links.filter((link) => link.target_type === "transaction").map((link) => link.target_id);
  const transactions = useQueries({
    queries: transactionIds.map((id) => ({
      queryKey: docKeys.linkedTransaction(id),
      queryFn: () => getLinkedTransaction(id),
      retry: false,
    })),
  });

  const describe = (link: DocumentLink) => {
    if (link.target_type === "account") {
      const account = accounts.data?.find((item) => item.id === link.target_id);
      if (account === undefined) {
        return accounts.isPending ? "Loading" : "An account you cannot see";
      }
      return (
        <Link to="/accounts/$accountId" params={{ accountId: account.id }}>
          {accountName(account)}
        </Link>
      );
    }
    if (link.target_type === "transaction") {
      const query = transactions.at(transactionIds.indexOf(link.target_id));
      if (query?.data !== undefined) {
        return <TransactionLine transaction={query.data} payees={payees.data} accounts={accounts.data} />;
      }
      return query?.isError === true ? "A transaction you cannot see, or that was deleted" : "Loading";
    }
    return "Shown where it belongs";
  };

  return (
    <Card title="Attached to">
      <div className="ledger-stack">
        {links.length === 0 ? (
          <p className="ledger-muted">
            Not attached to anything yet. Attach a receipt or statement to the transaction or account it belongs with, so the
            two are kept together.
          </p>
        ) : (
          <ul className="account-list" aria-label="Attached to">
            {links.map((link) => (
              <li key={link.id}>
                <span>
                  <Badge tone="neutral">{LINK_KIND[link.target_type]}</Badge> {describe(link)}
                </span>
                <span className="ledger-muted">Attached {formatDateTime(link.created_at) ?? link.created_at}</span>
              </li>
            ))}
          </ul>
        )}
        {document.can_edit ? (
          <div className="ledger-actions">
            <Button
              onClick={() => {
                setAttaching(true);
              }}
            >
              Attach to…
            </Button>
          </div>
        ) : null}
      </div>
      {attaching ? (
        <AttachDialog
          document={document}
          onClose={() => {
            setAttaching(false);
          }}
        />
      ) : null}
    </Card>
  );
}

function searchProblem(search: TransactionSearch): string | null {
  if (search.dateFrom !== "" && search.dateTo !== "" && compareIsoDates(search.dateFrom, search.dateTo) > 0) {
    return "The first date is after the last date.";
  }
  return null;
}

function FindTransaction({
  document,
  busyId,
  onAttach,
}: {
  document: DocumentDetail;
  busyId: string | null;
  onAttach: (transaction: LedgerTransaction, label: string) => void;
}) {
  const accounts = useAccounts();
  const payees = usePayees();
  // A statement's own period is the likeliest place to look.
  const initial: TransactionSearch = { q: "", dateFrom: document.period_start ?? "", dateTo: document.period_end ?? "" };
  const [draft, setDraft] = useState<TransactionSearch>(initial);
  const [applied, setApplied] = useState<TransactionSearch>(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const query = useInfiniteQuery({
    queryKey: docKeys.findTransactions(applied),
    queryFn: ({ pageParam }) => findTransactions(applied, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    // The dialog is inside the page; keep the search from submitting anything outside it.
    event.stopPropagation();
    const found = searchProblem(draft);
    setProblem(found);
    if (found === null) {
      setApplied({ ...draft, q: draft.q.trim() });
    }
  };
  return (
    <div className="ledger-stack">
      <form className="ledger-filters" role="search" aria-label="Find a transaction" noValidate onSubmit={submit}>
        <Field label="Payee, memo or note contains">
          <Input
            type="search"
            maxLength={100}
            value={draft.q}
            onChange={(event) => {
              setDraft({ ...draft, q: event.target.value });
            }}
          />
        </Field>
        <Field label="From date">
          <Input
            type="date"
            value={draft.dateFrom}
            onChange={(event) => {
              setDraft({ ...draft, dateFrom: event.target.value });
            }}
          />
        </Field>
        <Field label="To date">
          <Input
            type="date"
            value={draft.dateTo}
            onChange={(event) => {
              setDraft({ ...draft, dateTo: event.target.value });
            }}
          />
        </Field>
        <div>
          <Button type="submit">Search</Button>
        </div>
      </form>
      {problem === null ? null : (
        <p role="alert" className="ledger-error">
          {problem}
        </p>
      )}
      {query.isPending ? <Spinner label="Finding transactions" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : null}
      {query.isSuccess && rows.length === 0 ? (
        <p className="ledger-muted">No transactions match. Try fewer words or a wider range of dates.</p>
      ) : null}
      {rows.length === 0 ? null : (
        <ul className="account-list" aria-label="Matching transactions">
          {rows.map((transaction) => {
            const label = transactionLabel(transaction, payees.data);
            const editable = accounts.data?.find((account) => account.id === transaction.account_id)?.can_edit !== false;
            return (
              <li key={transaction.id}>
                <TransactionLine transaction={transaction} payees={payees.data} accounts={accounts.data} />
                {isLinked(document.links, "transaction", transaction.id) ? (
                  <Badge tone="success">Attached</Badge>
                ) : editable ? (
                  <Button
                    size="sm"
                    aria-label={`Attach to ${label}`}
                    loading={busyId === transaction.id}
                    disabled={busyId !== null && busyId !== transaction.id}
                    onClick={() => {
                      onAttach(transaction, label);
                    }}
                  >
                    Attach
                  </Button>
                ) : (
                  <span className="ledger-muted">You can only view this account</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {query.hasNextPage ? (
        <div>
          <Button loading={query.isFetchingNextPage} onClick={() => void query.fetchNextPage()}>
            Show more
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function ChooseAccount({
  document,
  busy,
  onAttach,
}: {
  document: DocumentDetail;
  busy: boolean;
  onAttach: (account: Account, label: string) => void;
}) {
  const accounts = useAccounts();
  const [chosen, setChosen] = useState("");
  const editable = (accounts.data ?? []).filter((account) => account.can_edit);
  const account = editable.find((item) => item.id === chosen);
  if (accounts.isPending) {
    return <Spinner label="Loading accounts" />;
  }
  if (accounts.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(accounts.error)}
      </p>
    );
  }
  if (editable.length === 0) {
    return <p className="ledger-muted">There is no account you may change, so there is nothing to attach this file to.</p>;
  }
  return (
    <div className="ledger-stack">
      <Field label="Account" hint="Only accounts you may change are listed.">
        <Select
          placeholder="Choose an account"
          options={editable.map((item) => {
            const linked = isLinked(document.links, "account", item.id);
            return { value: item.id, label: linked ? `${accountName(item)} (already attached)` : accountName(item), disabled: linked };
          })}
          value={chosen}
          onChange={(event) => {
            setChosen(event.target.value);
          }}
        />
      </Field>
      <div>
        <Button
          variant="primary"
          loading={busy}
          disabled={account === undefined}
          onClick={() => {
            if (account !== undefined) {
              onAttach(account, accountName(account));
            }
          }}
        >
          Attach to this account
        </Button>
      </div>
    </div>
  );
}

export function AttachDialog({ document, onClose }: { document: DocumentDetail; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const keys = useIdempotencyKey();
  const kindName = useId();
  const [kind, setKind] = useState<AttachTarget>("transaction");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const attach = async (type: AttachTarget, id: string, label: string): Promise<void> => {
    setBusyId(id);
    setError(null);
    try {
      await linkDocument(document.id, type, id, keys.keyFor(`${type}:${id}`));
      keys.done();
      notify({ tone: "success", title: "Document attached", description: label });
      await queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      onClose();
    } catch (caught) {
      setError(attachProblem(caught));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Attach to…"
      description={`Record what "${document.original_filename ?? "this file"}" belongs with. Nothing is imported or changed; the link is shown with the document.`}
      footer={<Button onClick={onClose}>Cancel</Button>}
    >
      <div className="ledger-stack">
        <fieldset className="scope-grid">
          <legend>Attach to</legend>
          <label>
            <input
              type="radio"
              name={kindName}
              value="transaction"
              checked={kind === "transaction"}
              onChange={() => {
                setKind("transaction");
                setError(null);
              }}
            />{" "}
            A transaction
          </label>
          <label>
            <input
              type="radio"
              name={kindName}
              value="account"
              checked={kind === "account"}
              onChange={() => {
                setKind("account");
                setError(null);
              }}
            />{" "}
            An account
          </label>
        </fieldset>
        <p className="ledger-muted">Check before you attach: an attachment cannot be removed again yet.</p>
        {kind === "transaction" ? (
          <FindTransaction
            document={document}
            busyId={busyId}
            onAttach={(transaction, label) => {
              void attach("transaction", transaction.id, label);
            }}
          />
        ) : (
          <ChooseAccount
            document={document}
            busy={busyId !== null}
            onAttach={(account, label) => {
              void attach("account", account.id, label);
            }}
          />
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
