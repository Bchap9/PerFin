// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts } from "../accounts/api";
import { getDocument, importKeys } from "../imports/api";
import { getTransaction, txKeys, type Transaction } from "../transactions/api";
import {
  accountResolution,
  dismissItem,
  duplicateResolution,
  listReviewItems,
  payloadIds,
  payloadString,
  payloadSuggestions,
  receiptResolution,
  reprocessResolution,
  resolveItem,
  reviewKeys,
  type ResolveBody,
  type ReviewFilter,
  type ReviewItem,
} from "./api";

const KIND_LABELS: Readonly<Record<ReviewItem["kind"], string>> = {
  unknown_owner: "Unknown owner",
  unknown_account: "Which account?",
  low_confidence_field: "Low confidence",
  possible_duplicate: "Possible duplicate",
  unparsed: "Could not be read",
  new_institution: "New institution",
  rejected_email: "Rejected email",
  receipt_match: "Attach this receipt",
};

const RECEIPT_REASONS: Readonly<Record<string, string>> = {
  ambiguous: "Several transactions fit this receipt equally well.",
  not_confident:
    "A transaction may fit this receipt, but not clearly enough to attach it automatically.",
  receipt_uncertain: "Parts of this receipt could not be read with confidence.",
  already_has_receipt: "The best fit already has another receipt attached.",
  no_candidates:
    "No transaction fits this receipt yet. It is attached automatically when its statement arrives.",
  no_total: "No total could be read on this receipt.",
};

const AMOUNT_LABELS: Readonly<Record<string, string>> = {
  exact: "same amount",
  refund: "refund of the same amount",
  tip: "amount plus a tip",
};

/** The matcher's ranked suggestions, one to choose. */
function ReceiptBody({
  item,
  chosen,
  onChoose,
}: {
  item: ReviewItem;
  chosen: string | null;
  onChoose: (id: string) => void;
}) {
  const reason = payloadString(item, "reason");
  const suggestions = payloadSuggestions(item);
  return (
    <div className="ledger-stack">
      <p>
        {(reason === null ? undefined : RECEIPT_REASONS[reason]) ??
          "Choose the transaction this receipt belongs to."}
      </p>
      {suggestions.length === 0 ? null : (
        <div role="radiogroup" aria-label="Suggested transactions">
          {suggestions.map((suggestion, index) => (
            <div key={suggestion.transactionId}>
              <TransactionLookup
                id={suggestion.transactionId}
                title={`Suggestion ${String(index + 1)}`}
              />
              <p className="ledger-muted">
                {AMOUNT_LABELS[suggestion.amount] ?? suggestion.amount}
                {suggestion.days === null
                  ? ""
                  : `, ${String(suggestion.days)} day(s) apart`}
                {suggestion.alreadyLinked ? ", already has a receipt" : ""}
              </p>
              {item.state === "open" && item.can_resolve ? (
                <label>
                  <input
                    type="radio"
                    name={`suggestion-${item.id}`}
                    checked={chosen === suggestion.transactionId}
                    onChange={() => {
                      onChoose(suggestion.transactionId);
                    }}
                  />{" "}
                  Attach to suggestion {index + 1}
                </label>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const FILTERS: readonly { value: ReviewFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "dismissed", label: "Dismissed" },
  { value: "all", label: "All" },
];

function isFilter(value: string): value is ReviewFilter {
  return FILTERS.some((filter) => filter.value === value);
}

function TransactionFacts({
  title,
  transaction,
}: {
  title: string;
  transaction: Transaction;
}) {
  return (
    <div>
      <h4>{title}</h4>
      <dl>
        <dt>Date</dt>
        <dd>
          {formatDate(transaction.posted_date) ?? transaction.posted_date}
        </dd>
        <dt>Payee</dt>
        <dd>{transaction.raw_payee ?? "-"}</dd>
        <dt>Memo</dt>
        <dd>{transaction.memo ?? "-"}</dd>
        <dt>Amount</dt>
        <dd>
          <MoneyText
            amount={transaction.amount.amount}
            currency={transaction.amount.currency}
          />
        </dd>
      </dl>
    </div>
  );
}

function TransactionLookup({ id, title }: { id: string; title: string }) {
  const query = useQuery({
    queryKey: txKeys.one(id),
    queryFn: () => getTransaction(id),
  });
  if (query.isPending) {
    return <Spinner label={`Loading ${title}`} />;
  }
  if (query.isError) {
    return (
      <div>
        <h4>{title}</h4>
        <p className="ledger-muted">No longer available.</p>
      </div>
    );
  }
  return <TransactionFacts title={title} transaction={query.data} />;
}

/** Side by side: the imported row against each kept candidate. */
function DuplicateBody({
  item,
  chosen,
  onChoose,
}: {
  item: ReviewItem;
  chosen: string | null;
  onChoose: (id: string) => void;
}) {
  const transactionId = payloadString(item, "transaction_id");
  const candidates = payloadIds(item, "candidates");
  const documentId = item.document_id;
  const document = useQuery({
    queryKey: importKeys.document(documentId ?? ""),
    queryFn: () => getDocument(documentId ?? ""),
    enabled: documentId !== null && transactionId === null,
  });
  if (transactionId === null) {
    return (
      <p>
        The file {document.data?.original_filename ?? "you uploaded"} has the
        same content as one already stored. Keeping both would import its rows
        twice.
      </p>
    );
  }
  return (
    <div
      className="side-by-side"
      role="group"
      aria-label="Compare transactions"
    >
      <TransactionLookup id={transactionId} title="Imported row" />
      {candidates.map((candidate, index) => (
        <div key={candidate}>
          <TransactionLookup
            id={candidate}
            title={
              candidates.length > 1
                ? `Existing row ${String(index + 1)}`
                : "Existing row"
            }
          />
          {candidates.length > 1 ? (
            <label>
              <input
                type="radio"
                name={`candidate-${item.id}`}
                checked={chosen === candidate}
                onChange={() => {
                  onChoose(candidate);
                }}
              />{" "}
              Same as existing row {index + 1}
            </label>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function ReviewItemCard({ item }: { item: ReviewItem }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const accounts = useAccounts();
  const [accountId, setAccountId] = useState("");
  const [remember, setRemember] = useState(true);
  const [chosen, setChosen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const open = item.state === "open";
  const actionable = open && item.can_resolve;
  const candidates = payloadIds(item, "candidates");

  const done = (title: string): void => {
    void queryClient.invalidateQueries({ queryKey: queryRoots.review });
    void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
    void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
    toast.notify({ title, tone: "success" });
  };
  const resolve = useMutation({
    mutationFn: (body: ResolveBody) => resolveItem(item.id, body),
    onSuccess: () => {
      setError(null);
      done("Item resolved");
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const dismiss = useMutation({
    mutationFn: () => dismissItem(item.id),
    onSuccess: () => {
      setError(null);
      done("Item dismissed");
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const busy = resolve.isPending || dismiss.isPending;
  const needsChoice = candidates.length > 1 && chosen === null;
  const editableAccounts = (accounts.data ?? []).filter(
    (account) => account.can_edit && !account.is_closed,
  );

  return (
    <Card
      title={KIND_LABELS[item.kind]}
      actions={
        open ? undefined : (
          <Badge tone={item.state === "resolved" ? "success" : "neutral"}>
            {item.state === "resolved" ? "Resolved" : "Dismissed"}
          </Badge>
        )
      }
    >
      <article className="review-item" aria-label={KIND_LABELS[item.kind]}>
        {item.kind === "possible_duplicate" ? (
          <DuplicateBody item={item} chosen={chosen} onChoose={setChosen} />
        ) : null}
        {item.kind === "unknown_account" ? (
          <>
            <p>PerFin could not tell which account this file belongs to.</p>
            {actionable ? (
              <>
                <Field label="Account for this file">
                  <Select
                    placeholder="Choose an account"
                    options={editableAccounts.map((account) => ({
                      value: account.id,
                      label: accountLabel(account),
                    }))}
                    value={accountId}
                    onChange={(event) => {
                      setAccountId(event.target.value);
                    }}
                  />
                </Field>
                <Checkbox
                  label="Remember this account for the next file"
                  checked={remember}
                  onChange={(event) => {
                    setRemember(event.target.checked);
                  }}
                />
              </>
            ) : null}
          </>
        ) : null}
        {item.kind === "unparsed" ? (
          <p>
            {item.next_step === "column_mapping"
              ? "This file needs a column mapping before its rows can be imported."
              : "This file could not be read. You can try processing it again."}
          </p>
        ) : null}
        {item.kind === "receipt_match" ? (
          <ReceiptBody item={item} chosen={chosen} onChoose={setChosen} />
        ) : null}
        {item.kind !== "possible_duplicate" &&
        item.kind !== "unknown_account" &&
        item.kind !== "unparsed" &&
        item.kind !== "receipt_match" ? (
          <p className="ledger-muted">
            This item can only be dismissed for now.
          </p>
        ) : null}
        {error === null ? null : (
          <p role="alert" className="ledger-error">
            {error}
          </p>
        )}
        {actionable ? (
          <div className="ledger-actions">
            {item.kind === "possible_duplicate" ? (
              <>
                <Button
                  variant="primary"
                  disabled={needsChoice}
                  loading={resolve.isPending}
                  onClick={() => {
                    resolve.mutate(
                      duplicateResolution(item, "confirm", chosen),
                    );
                  }}
                >
                  {payloadString(item, "transaction_id") === null
                    ? "Reject this upload"
                    : "It is a duplicate"}
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => {
                    resolve.mutate(duplicateResolution(item, "reject", null));
                  }}
                >
                  Not a duplicate
                </Button>
              </>
            ) : null}
            {item.kind === "receipt_match" ? (
              <>
                <Button
                  variant="primary"
                  disabled={chosen === null}
                  loading={resolve.isPending}
                  onClick={() => {
                    resolve.mutate(receiptResolution(chosen));
                  }}
                >
                  Attach receipt
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => {
                    resolve.mutate(receiptResolution(null));
                  }}
                >
                  No transaction
                </Button>
              </>
            ) : null}
            {item.kind === "unknown_account" ? (
              <Button
                variant="primary"
                disabled={accountId === ""}
                loading={resolve.isPending}
                onClick={() => {
                  resolve.mutate(accountResolution(accountId, remember));
                }}
              >
                Import into this account
              </Button>
            ) : null}
            {item.kind === "unparsed" &&
            item.next_step === "column_mapping" &&
            item.document_id !== null ? (
              <Link
                to="/documents/$documentId/mapping"
                params={{ documentId: item.document_id }}
                className="btn btn--primary btn--md"
              >
                Map columns
              </Link>
            ) : null}
            {item.kind === "unparsed" ? (
              <Button
                loading={resolve.isPending}
                onClick={() => {
                  resolve.mutate(reprocessResolution());
                }}
              >
                Process again
              </Button>
            ) : null}
            <Button
              variant="ghost"
              loading={dismiss.isPending}
              disabled={resolve.isPending}
              onClick={() => {
                dismiss.mutate();
              }}
            >
              Dismiss
            </Button>
          </div>
        ) : null}
      </article>
    </Card>
  );
}

export function ReviewPage() {
  const [filter, setFilter] = useState<ReviewFilter>("open");
  const query = useQuery({
    queryKey: reviewKeys.list(filter),
    queryFn: () => listReviewItems(filter),
  });
  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Review</h1>
        <Field label="Show">
          <Select
            options={FILTERS}
            value={filter}
            onChange={(event) => {
              if (isFilter(event.target.value)) {
                setFilter(event.target.value);
              }
            }}
          />
        </Field>
      </div>
      {query.isPending ? <Spinner label="Loading review items" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : null}
      {query.isSuccess && query.data.length === 0 ? (
        <Card>
          <EmptyState
            title="Nothing to review"
            description="Items from imports that need a decision show up here."
          />
        </Card>
      ) : null}
      {query.data?.map((item) => (
        <ReviewItemCard key={item.id} item={item} />
      ))}
    </section>
  );
}
