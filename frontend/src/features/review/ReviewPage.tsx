// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate, formatDateTime } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts } from "../accounts/api";
import { PageViewer } from "../documents/PageViewer";
import { getDocument, importKeys } from "../imports/api";
import { DocumentAccountsDialog } from "../imports/DocumentAccountsDialog";
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
import { explain, payloadPeriod, type Explanation } from "./reviewCopy";
import { TeachDialog, currentReadings } from "./TeachDialog";
import {
  BalanceTools,
  CostBasisTools,
  LowConfidenceTools,
  NewInstitutionTools,
  ReceiptPreview,
  UnknownOwnerTools,
} from "./ReviewTools";

const RECEIPT_REASONS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    ambiguous: "Several transactions fit this receipt equally well.",
    not_confident:
      "A transaction may fit this receipt, but not clearly enough to attach it automatically.",
    receipt_uncertain:
      "Parts of this receipt could not be read with confidence.",
    already_has_receipt: "The best fit already has another receipt attached.",
    no_candidates:
      "No transaction fits this receipt yet. It is attached automatically when its statement arrives.",
    no_total: "No total could be read on this receipt.",
  }),
);

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
        {(reason === null ? undefined : RECEIPT_REASONS.get(reason)) ??
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
/** A statement whose period overlaps one already imported (``statement_overlap``). */
function isStatementOverlap(item: ReviewItem): boolean {
  return (item.reason ?? payloadString(item, "reason")) === "statement_overlap";
}

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
  const overlap = isStatementOverlap(item);
  const otherId = payloadString(item, "other_document_id");
  const document = useQuery({
    queryKey: importKeys.document(documentId ?? ""),
    queryFn: () => getDocument(documentId ?? ""),
    enabled: documentId !== null && transactionId === null && !overlap,
  });
  const other = useQuery({
    queryKey: importKeys.document(otherId ?? ""),
    queryFn: () => getDocument(otherId ?? ""),
    enabled: overlap && otherId !== null,
    retry: false,
  });
  if (overlap) {
    const period = payloadPeriod(item, "period_start", "period_end");
    const otherPeriod = payloadPeriod(
      item,
      "other_period_start",
      "other_period_end",
    );
    return (
      <div role="group" aria-label="Compare statements">
        <p>
          This statement{period === null ? "" : ` (${period})`} overlaps one
          already imported for the account
          {otherPeriod === null ? "" : ` (${otherPeriod})`}.
        </p>
        {otherId === null ? null : (
          <p>
            Imported from{" "}
            <Link to="/documents/$documentId" params={{ documentId: otherId }}>
              {other.data?.original_filename ?? "another file"}
            </Link>
            .
          </p>
        )}
      </div>
    );
  }
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

const SOURCES: Readonly<Record<string, string>> = {
  upload: "uploaded",
  dropfolder: "from the drop folder",
  email: "by email",
  archive: "from an archive",
};

/** The file an item is about: its name (a link), when it arrived, its period. */
export function DocumentLine({ item }: { item: ReviewItem }) {
  const document = item.document;
  if (document === null || document === undefined) {
    return null;
  }
  const period =
    document.period_start !== null && document.period_end !== null
      ? `${formatDate(document.period_start) ?? document.period_start} to ${formatDate(document.period_end) ?? document.period_end}`
      : null;
  return (
    <p className="review-document">
      <Link to="/documents/$documentId" params={{ documentId: document.id }}>
        {document.original_filename ?? "Unnamed file"}
      </Link>
      <span className="ledger-muted">
        {" "}
        {SOURCES[document.source] ?? ""}{" "}
        {formatDateTime(document.created_at) ?? ""}
        {period === null ? "" : `, covering ${period}`}
      </span>
    </p>
  );
}

/** A page preview shown on request (only for files whose pages the caller may see). */
function DocumentPeek({ item }: { item: ReviewItem }) {
  const [shown, setShown] = useState(false);
  if (item.document?.previewable !== true || item.kind === "receipt_match") {
    return null;
  }
  return (
    <div className="ledger-stack">
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          setShown(!shown);
        }}
      >
        {shown ? "Hide document" : "Show document"}
      </Button>
      {shown ? <PageViewer documentId={item.document.id} /> : null}
    </div>
  );
}

/** Why the item exists and what to do, as a short list of steps. */
export function ExplanationBlock({
  explanation,
  full,
}: {
  explanation: Explanation;
  full: boolean;
}) {
  return (
    <div className="review-explanation">
      <p>{explanation.what}</p>
      {full && explanation.why !== undefined ? (
        <p className="ledger-muted">{explanation.why}</p>
      ) : null}
      {full ? (
        <>
          <h4>How to fix it</h4>
          <ol>
            {explanation.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </>
      ) : null}
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
  const [fixing, setFixing] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const [teaching, setTeaching] = useState(false);
  const explanation = explain(item);
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
      setFixing(false);
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
      setFixing(false);
      done("Item dismissed");
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const busy = resolve.isPending || dismiss.isPending;
  // Any item about a page the caller may see can be taught from.
  const teachable =
    item.document_id !== null && item.document?.previewable === true;
  const needsChoice = candidates.length > 1 && chosen === null;
  const editableAccounts = (accounts.data ?? []).filter(
    (account) => account.can_edit && !account.is_closed,
  );

  const controls = (
    <>
      {item.kind === "possible_duplicate" ? (
        <DuplicateBody item={item} chosen={chosen} onChoose={setChosen} />
      ) : null}
      {item.kind === "unknown_account" ? (
        <>
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
      {item.kind === "receipt_match" ? (
        <>
          <ReceiptBody item={item} chosen={chosen} onChoose={setChosen} />
          <ReceiptPreview item={item} />
        </>
      ) : null}
      {item.kind === "low_confidence_field" &&
      item.reason === "investment_opening_basis_unknown" ? (
        <CostBasisTools item={item} actionable={actionable} />
      ) : null}
      {item.kind === "low_confidence_field" &&
      (item.reason === "reconciliation_failed" ||
        item.reason === "reconciliation_unavailable") ? (
        <BalanceTools item={item} actionable={actionable} />
      ) : null}
      {item.kind === "low_confidence_field" ? (
        <LowConfidenceTools item={item} actionable={actionable} />
      ) : null}
      {item.kind === "new_institution" ? (
        <NewInstitutionTools item={item} actionable={actionable} />
      ) : null}
      {item.kind === "unknown_owner" ? (
        <UnknownOwnerTools item={item} actionable={actionable} />
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
                  resolve.mutate(duplicateResolution(item, "confirm", chosen));
                }}
              >
                {isStatementOverlap(item)
                  ? "Same statement — skip"
                  : payloadString(item, "transaction_id") === null
                    ? "Reject this upload"
                    : "It is a duplicate"}
              </Button>
              <Button
                disabled={busy}
                onClick={() => {
                  resolve.mutate(duplicateResolution(item, "reject", null));
                }}
              >
                {isStatementOverlap(item) ? "Import anyway" : "Not a duplicate"}
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
          {item.kind === "unknown_account" && item.document_id !== null ? (
            <Button
              onClick={() => {
                setFixing(false);
                setSettingUp(true);
              }}
            >
              Set up accounts from this file
            </Button>
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
          {teachable ? (
            <Button
              onClick={() => {
                setFixing(false);
                setTeaching(true);
              }}
            >
              Teach PerFin
            </Button>
          ) : null}
          {(item.next_step === "column_mapping" ||
            item.reason === "investment_low_confidence") &&
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
    </>
  );

  return (
    <Card
      title={explanation.title}
      actions={
        open ? (
          <Button
            size="sm"
            onClick={() => {
              setFixing(true);
            }}
          >
            {actionable ? "Fix…" : "Details"}
          </Button>
        ) : (
          <Badge tone={item.state === "resolved" ? "success" : "neutral"}>
            {item.state === "resolved" ? "Resolved" : "Dismissed"}
          </Badge>
        )
      }
    >
      <article className="review-item" aria-label={explanation.title}>
        <DocumentLine item={item} />
        <ExplanationBlock explanation={explanation} full={false} />
        {fixing ? null : (
          <>
            <DocumentPeek item={item} />
            {controls}
          </>
        )}
      </article>
      {teaching && item.document_id !== null ? (
        <TeachDialog
          documentId={item.document_id}
          item={item}
          readings={currentReadings(item.document, item)}
          onClose={() => {
            setTeaching(false);
          }}
        />
      ) : null}
      {settingUp && item.document_id !== null ? (
        <DocumentAccountsDialog
          documentId={item.document_id}
          onClose={() => {
            setSettingUp(false);
          }}
        />
      ) : null}
      {fixing ? (
        <Dialog
          open={fixing}
          onClose={() => {
            setFixing(false);
          }}
          title={explanation.title}
          size="lg"
        >
          <div className="review-fix">
            {item.document?.previewable === true &&
            item.document_id !== null ? (
              <div className="review-fix__document">
                <PageViewer documentId={item.document_id} />
              </div>
            ) : null}
            <div className="review-fix__steps ledger-stack">
              <DocumentLine item={item} />
              <ExplanationBlock explanation={explanation} full />
              {controls}
            </div>
          </div>
        </Dialog>
      ) : null}
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
