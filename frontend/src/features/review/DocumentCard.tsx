// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One card per document in Review: the file, what waits on it (one section
 * per finding) and the one thing to do next, as the server names it
 * (`card.action` for `card.action_item_id`). The other sections explain and
 * can be dismissed; they get their turn when the list reloads.
 */
import "../accounts/ledger.css";
import "./review.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useState, type ReactNode } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { formatDate, formatDateTime } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts } from "../accounts/api";
import { docKeys, getImported, type DocumentImported } from "../documents/api";
import { PageViewer } from "../documents/PageViewer";
import { getDocument, importKeys } from "../imports/api";
import { DocumentAccountsDialog } from "../imports/DocumentAccountsDialog";
import { getTransaction, txKeys, type Transaction } from "../transactions/api";
import {
  accountResolution,
  canImportAsRead,
  dismissItem,
  duplicateResolution,
  fixFromDraft,
  getReviewItem,
  hasSuggestedKinds,
  importAsReadResolution,
  itemReason,
  lineFixable,
  lineFixResolution,
  linesSelectable,
  listsTransactions,
  nearMatchesResolution,
  payloadIds,
  resemblesRemoved,
  payloadLines,
  payloadNearMatches,
  payloadNumber,
  payloadString,
  payloadSuggestions,
  receiptResolution,
  reprocessResolution,
  reviewKeys,
  signResolution,
  type CardAction,
  type DuplicateDecision,
  type LineDraft,
  type LineFix,
  type NearMatch,
  type ReviewCard,
  type ReviewDocument,
  type ReviewItem,
} from "./api";
import { ReconcileView } from "./ReconcileView";
import { explain, payloadPeriod, type Explanation } from "./reviewCopy";
import {
  CostBasisTools,
  HeldLines,
  LowConfidenceTools,
  NewInstitutionTools,
  ReceiptPreview,
  UnknownOwnerTools,
  useItemDetail,
  useResolve,
} from "./ReviewTools";
import { TeachDialog, currentReadings } from "./TeachDialog";

// ---- the file and the words ----------------------------------------------------

const SOURCES: ReadonlyMap<string, string> = new Map(
  Object.entries({
    upload: "uploaded",
    dropfolder: "from the drop folder",
    email: "by email",
    archive: "from an archive",
  }),
);

/** The file a card is about: its name (a link), when it arrived, its period. */
export function DocumentLine({
  document,
}: {
  document: ReviewDocument | null | undefined;
}) {
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
        {SOURCES.get(document.source) ?? ""}{" "}
        {formatDateTime(document.created_at) ?? ""}
        {period === null ? "" : `, covering ${period}`}
      </span>
    </p>
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

function Refusal({ error }: { error: string | null }) {
  return error === null ? null : (
    <p role="alert" className="ledger-error">
      {error}
    </p>
  );
}

// ---- rows of the ledger, side by side ------------------------------------------

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

/** A statement whose period overlaps one already imported (``statement_overlap``). */
function isStatementOverlap(item: ReviewItem): boolean {
  return itemReason(item) === "statement_overlap";
}

/**
 * What a duplicate item is about: two statements, a repeated file, or the
 * imported row against each kept candidate (``onChoose``: which one it
 * repeats, when there are several).
 */
function DuplicateBody({
  item,
  chosen,
  onChoose,
}: {
  item: ReviewItem;
  chosen: string | null;
  onChoose?: (id: string) => void;
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
          {candidates.length > 1 && onChoose !== undefined ? (
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

// ---- the one thing to do next, per action --------------------------------------

interface ControlProps {
  item: ReviewItem;
  /** The item is open and the caller may resolve it. */
  actionable: boolean;
  /** The section's other buttons ("Dismiss"), shown after the control's own. */
  trailing: ReactNode;
}

function Actions({ children }: { children: ReactNode }) {
  return <div className="ledger-actions">{children}</div>;
}

/** `choose_account`: the account, pre-selected when one clearly ranks first. */
function AccountControl({ item, actionable, trailing }: ControlProps) {
  const accounts = useAccounts();
  const detail = useItemDetail(item, actionable);
  const [picked, setPicked] = useState<string | null>(null);
  // Untouched, "Remember" follows who chose the account: ticked for the
  // person's own choice, not for PerFin's guess, which one click would
  // otherwise turn into the route every later file takes.
  const [remembered, setRemembered] = useState<boolean | null>(null);
  const [settingUp, setSettingUp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  if (!actionable) {
    return null;
  }
  const editable = (accounts.data ?? []).filter(
    (account) => account.can_edit && !account.is_closed,
  );
  // The server ranks the accounts best fit first. One is chosen for the
  // person only when it is strictly ahead of the next (or the only one).
  const ranked = (detail.data?.alternatives ?? []).filter(
    (alt) =>
      alt.field === "account_id" &&
      editable.some((account) => account.id === alt.value),
  );
  const [first, second] = ranked;
  const ahead =
    first !== undefined &&
    (second === undefined || (first.score ?? 0) > (second.score ?? 0));
  const accountId = picked ?? (ahead ? first.value : "");
  const remember = remembered ?? picked !== null;
  return (
    <>
      <Field label="Account for this file">
        <Select
          placeholder="Choose an account"
          options={editable.map((account) => ({
            value: account.id,
            label: accountLabel(account),
          }))}
          value={accountId}
          onChange={(event) => {
            setPicked(event.target.value);
          }}
        />
      </Field>
      <Checkbox
        label="Remember this account for the next file"
        checked={remember}
        onChange={(event) => {
          setRemembered(event.target.checked);
        }}
      />
      <Refusal error={error} />
      <Actions>
        <Button
          variant="primary"
          disabled={accountId === ""}
          loading={resolve.isPending}
          onClick={() => {
            resolve.mutate(accountResolution(accountId, remember));
          }}
        >
          Use this account
        </Button>
        {item.document_id === null ? null : (
          <Button
            onClick={() => {
              setSettingUp(true);
            }}
          >
            Set up accounts from this file
          </Button>
        )}
        {trailing}
      </Actions>
      {settingUp && item.document_id !== null ? (
        <DocumentAccountsDialog
          documentId={item.document_id}
          onClose={() => {
            setSettingUp(false);
          }}
        />
      ) : null}
    </>
  );
}

/**
 * `answer_sign`: what a positive amount of the file means. Neither answer
 * is the primary button: PerFin has no reason to lean either way.
 */
function SignControl({ item, actionable, trailing }: ControlProps) {
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const questionId = useId();
  if (!actionable) {
    return null;
  }
  return (
    <div role="group" aria-labelledby={questionId} className="ledger-stack">
      <p id={questionId}>Positive amounts in this file are:</p>
      <Actions>
        <Button
          disabled={resolve.isPending}
          onClick={() => {
            resolve.mutate(signResolution("money_in"));
          }}
        >
          Money in
        </Button>
        <Button
          disabled={resolve.isPending}
          onClick={() => {
            resolve.mutate(signResolution("money_out"));
          }}
        >
          Money out
        </Button>
        {trailing}
      </Actions>
      <p className="ledger-muted">
        {payloadString(item, "scope") === "mapping"
          ? "PerFin remembers the answer for files with the same columns."
          : "The answer is kept for this account in this file."}
      </p>
      <Refusal error={error} />
    </div>
  );
}

function MapColumnsLink({ documentId }: { documentId: string }) {
  return (
    <Link
      to="/documents/$documentId/mapping"
      params={{ documentId }}
      className="btn btn--primary btn--md"
    >
      Map columns
    </Link>
  );
}

/** `confirm_duplicate`: a repeated statement, file or row. */
function DuplicateControl({ item, actionable, trailing }: ControlProps) {
  const [chosen, setChosen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const candidates = payloadIds(item, "candidates");
  const overlap = isStatementOverlap(item);
  const removed = resemblesRemoved(item);
  return (
    <>
      <DuplicateBody
        item={item}
        chosen={chosen}
        {...(actionable ? { onChoose: setChosen } : {})}
      />
      <Refusal error={error} />
      {actionable ? (
        <Actions>
          <Button
            variant="primary"
            disabled={candidates.length > 1 && chosen === null}
            loading={resolve.isPending}
            onClick={() => {
              resolve.mutate(duplicateResolution(item, "confirm", chosen));
            }}
          >
            {overlap
              ? "Same statement — skip"
              : payloadString(item, "transaction_id") === null
                ? "Reject this upload"
                : removed
                  ? "Remove it again"
                  : "It is a duplicate"}
          </Button>
          <Button
            disabled={resolve.isPending}
            onClick={() => {
              resolve.mutate(duplicateResolution(item, "reject", null));
            }}
          >
            {overlap ? "Import anyway" : removed ? "Keep it" : "Not a duplicate"}
          </Button>
          {trailing}
        </Actions>
      ) : null}
    </>
  );
}

/** What a person said about one row of a `near_matches` item. */
interface NearAnswer {
  decision: "confirm" | "reject";
  /** The row already in the account it repeats (needed with several). */
  duplicateOf: string | null;
}

function nearDecision(
  row: NearMatch,
  answer: NearAnswer,
): DuplicateDecision | null {
  if (answer.decision === "reject") {
    return { transaction_id: row.transactionId, decision: "reject" };
  }
  if (row.candidates.length === 0) {
    // It names no row to repeat: "confirm" removes it again.
    return { transaction_id: row.transactionId, decision: "confirm" };
  }
  const duplicateOf =
    answer.duplicateOf ??
    (row.candidates.length === 1 ? (row.candidates[0] ?? null) : null);
  return duplicateOf === null
    ? null
    : {
        transaction_id: row.transactionId,
        decision: "confirm",
        duplicate_of: duplicateOf,
      };
}

function NearRow({
  row,
  index,
  answer,
  actionable,
  onAnswer,
}: {
  row: NearMatch;
  index: number;
  answer: NearAnswer | undefined;
  actionable: boolean;
  onAnswer: (answer: NearAnswer) => void;
}) {
  const name = `near-${row.transactionId}`;
  const several = row.candidates.length > 1;
  // No row already in the account: it looks like one the person deleted.
  const removed = row.candidates.length === 0;
  return (
    <li>
      <div
        className="review-near__row"
        role="group"
        aria-label={`Row ${String(index + 1)}`}
      >
        <div className="side-by-side">
          <TransactionLookup id={row.transactionId} title="Row of this file" />
          {removed ? (
            <p className="ledger-muted">
              Has the date and amount of a row you deleted from this account.
            </p>
          ) : null}
          {row.candidates.map((candidate, place) => (
            <div key={candidate}>
              <TransactionLookup
                id={candidate}
                title={
                  several
                    ? `Already in PerFin ${String(place + 1)}`
                    : "Already in PerFin"
                }
              />
              {several && actionable ? (
                <label>
                  <input
                    type="radio"
                    name={`${name}-of`}
                    checked={
                      answer?.decision === "confirm" &&
                      answer.duplicateOf === candidate
                    }
                    onChange={() => {
                      onAnswer({ decision: "confirm", duplicateOf: candidate });
                    }}
                  />{" "}
                  Same row as this one
                </label>
              ) : null}
            </div>
          ))}
        </div>
        {actionable ? (
          <div className="review-near__answer">
            <label>
              <input
                type="radio"
                name={name}
                checked={answer?.decision === "confirm"}
                onChange={() => {
                  onAnswer({
                    decision: "confirm",
                    duplicateOf: answer?.duplicateOf ?? null,
                  });
                }}
              />{" "}
              {removed ? "Remove it again" : "Same row"}
            </label>
            <label>
              <input
                type="radio"
                name={name}
                checked={answer?.decision === "reject"}
                onChange={() => {
                  onAnswer({ decision: "reject", duplicateOf: null });
                }}
              />{" "}
              {removed ? "Keep it" : "Different row"}
            </label>
            {several &&
            answer?.decision === "confirm" &&
            answer.duplicateOf === null ? (
              <span className="ledger-muted">
                Choose which row already in PerFin it repeats.
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

/**
 * `confirm_duplicate` for `near_matches`: every listed row answered in one
 * request. A row left without an answer stays in the item.
 */
function NearMatchesControl({ item, actionable, trailing }: ControlProps) {
  const rows = payloadNearMatches(item);
  const [answers, setAnswers] = useState<ReadonlyMap<string, NearAnswer>>(
    new Map(),
  );
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const decisions: DuplicateDecision[] = [];
  let incomplete = false;
  for (const row of rows) {
    const answer = answers.get(row.transactionId);
    if (answer === undefined) {
      continue;
    }
    const decision = nearDecision(row, answer);
    if (decision === null) {
      incomplete = true;
    } else {
      decisions.push(decision);
    }
  }
  const listed = payloadNumber(item, "count") ?? rows.length;
  return (
    <>
      <ol className="review-near">
        {rows.map((row, index) => (
          <NearRow
            key={row.transactionId}
            row={row}
            index={index}
            answer={answers.get(row.transactionId)}
            actionable={actionable}
            onAnswer={(answer) => {
              setAnswers((before) =>
                new Map(before).set(row.transactionId, answer),
              );
            }}
          />
        ))}
      </ol>
      {listed > rows.length ? (
        <p className="ledger-muted">
          {`${String(listed - rows.length)} more row(s) are asked about once these are answered.`}
        </p>
      ) : null}
      <Refusal error={error} />
      {actionable ? (
        <Actions>
          <Button
            variant="primary"
            disabled={decisions.length === 0 || incomplete}
            loading={resolve.isPending}
            onClick={() => {
              resolve.mutate(nearMatchesResolution(decisions));
            }}
          >
            Save these answers
          </Button>
          <Button
            disabled={resolve.isPending}
            onClick={() => {
              // A row that names no twin is not "the same row" as anything:
              // it keeps the answer it has (or none).
              setAnswers((before) => {
                const next = new Map(before);
                for (const row of rows) {
                  if (row.candidates.length > 0) {
                    next.set(row.transactionId, {
                      decision: "confirm",
                      duplicateOf:
                        before.get(row.transactionId)?.duplicateOf ?? null,
                    });
                  }
                }
                return next;
              });
            }}
          >
            All are the same rows
          </Button>
          {trailing}
        </Actions>
      ) : null}
    </>
  );
}

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

const AMOUNT_LABELS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    exact: "same amount",
    refund: "refund of the same amount",
    tip: "amount plus a tip",
  }),
);

/** `attach_receipt`: the matcher's ranked suggestions, one to choose. */
function ReceiptControl({ item, actionable, trailing }: ControlProps) {
  const [chosen, setChosen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const reason = payloadString(item, "reason");
  const suggestions = payloadSuggestions(item);
  return (
    <>
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
                  {AMOUNT_LABELS.get(suggestion.amount) ?? suggestion.amount}
                  {suggestion.days === null
                    ? ""
                    : `, ${String(suggestion.days)} day(s) apart`}
                  {suggestion.alreadyLinked ? ", already has a receipt" : ""}
                </p>
                {actionable ? (
                  <label>
                    <input
                      type="radio"
                      name={`suggestion-${item.id}`}
                      checked={chosen === suggestion.transactionId}
                      onChange={() => {
                        setChosen(suggestion.transactionId);
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
      <ReceiptPreview item={item} />
      <Refusal error={error} />
      {actionable ? (
        <Actions>
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
            disabled={resolve.isPending}
            onClick={() => {
              resolve.mutate(receiptResolution(null));
            }}
          >
            No transaction
          </Button>
          {trailing}
        </Actions>
      ) : null}
    </>
  );
}

/** `reprocess`: read the file again. */
function ReprocessControl({ item, actionable, trailing }: ControlProps) {
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  if (!actionable) {
    return null;
  }
  return (
    <>
      <Refusal error={error} />
      <Actions>
        <Button
          variant="primary"
          loading={resolve.isPending}
          onClick={() => {
            resolve.mutate(reprocessResolution());
          }}
        >
          Process again
        </Button>
        {trailing}
      </Actions>
    </>
  );
}

function useDismiss(item: ReviewItem, onError: (message: string | null) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => dismissItem(item.id),
    onSuccess: () => {
      onError(null);
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
    },
    onError: (caught) => {
      onError(errorMessage(caught));
    },
  });
}

/**
 * `acknowledge`: nothing can be done about the item but to have seen it.
 * "OK" closes it. A copy PerFin skipped can still be read ("Import it anyway").
 * "Dismiss" (`trailing`) is shown only for the copy, where "OK" is an answer
 * of its own; elsewhere "OK" is the dismissal and a second button would do
 * the same thing.
 */
function AcknowledgeControl({ item, actionable, trailing }: ControlProps) {
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const dismiss = useDismiss(item, setError);
  const copy =
    item.kind === "possible_duplicate" &&
    itemReason(item) === "same_content_skipped";
  if (!actionable) {
    return null;
  }
  return (
    <>
      <Refusal error={error} />
      <Actions>
        <Button
          variant="primary"
          loading={copy ? resolve.isPending : dismiss.isPending}
          onClick={() => {
            if (copy) {
              resolve.mutate(duplicateResolution(item, "confirm", null));
            } else {
              dismiss.mutate();
            }
          }}
        >
          OK
        </Button>
        {copy ? (
          <Button
            disabled={resolve.isPending}
            onClick={() => {
              resolve.mutate(duplicateResolution(item, "reject", null));
            }}
          >
            Import it anyway
          </Button>
        ) : null}
        {copy ? trailing : null}
      </Actions>
    </>
  );
}

const HELD_REASONS: ReadonlySet<string> = new Set([
  "investment_reconciliation_failed",
  "investment_low_confidence",
  "investment_lines_held",
  "investment_figures_differ",
]);

/**
 * A brokerage statement held back by its figures: its rows are not in the
 * ledger yet, so there is nothing to confirm row by row.
 */
function isHeldStatement(item: ReviewItem): boolean {
  return (
    item.kind === "low_confidence_field" &&
    HELD_REASONS.has(itemReason(item) ?? "")
  );
}

function asReadNote(item: ReviewItem): string {
  if (hasSuggestedKinds(item)) {
    return "“Import as read” confirms the kinds the local model suggested for the lines listed above and imports them as printed. Headings and broker wording you confirm are remembered under “What PerFin has learned”; nothing else of the statement is.";
  }
  const reason = itemReason(item);
  if (reason === "investment_lines_held") {
    return "“Import as read” imports the lines listed above as printed; the rest of the statement is already imported.";
  }
  return reason === "investment_reconciliation_failed"
    ? "“Import as read” imports this statement as printed. PerFin records no account value from it, because its totals do not add up."
    : "“Import as read” imports this statement as printed. PerFin records no account value from it, because its totals could not be checked.";
}

/**
 * The held lines of a brokerage statement: imported as read (all, or the
 * ticked ones where they stand alone), or corrected first where a line's
 * doubt is a value a person can type.
 */
function HeldControl({ item, actionable, trailing }: ControlProps) {
  const [unticked, setUnticked] = useState<ReadonlySet<number>>(new Set());
  const [drafts, setDrafts] = useState<ReadonlyMap<number, LineDraft>>(
    new Map(),
  );
  // Lines the person said to leave out while correcting others.
  const [left, setLeft] = useState<ReadonlySet<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const lines = payloadLines(item);
  if (!actionable) {
    return <HeldLines item={item} />;
  }
  const asRead = canImportAsRead(item);
  // Lines that stand alone (none moves shares) are imported or left out one
  // by one: all ticked to begin with.
  const selectable = asRead && linesSelectable(item);
  const listed = lines.map((line) => line.index);
  const ticked = new Set(listed.filter((index) => !unticked.has(index)));
  const someLines = selectable && ticked.size < listed.length;
  const currency = payloadString(item, "currency");
  const correctable = lines.some(lineFixable);
  const fixes: LineFix[] = [];
  let invalid = false;
  // Lines neither corrected nor left out. Importing the corrected lines
  // drops every other line of the item for good, so none may be undecided.
  let undecided = 0;
  for (const line of lines) {
    const draft = drafts.get(line.index);
    const fix =
      draft === undefined ? null : fixFromDraft(line, draft, currency);
    if (fix === "invalid") {
      invalid = true;
    } else if (fix !== null) {
      fixes.push(fix);
    } else if (!left.has(line.index)) {
      undecided += 1;
    }
  }
  const correcting = correctable && !asRead;
  const setLeaving = (index: number, chosen: boolean): void => {
    setLeft((before) => {
      const next = new Set(before);
      if (chosen) {
        next.add(index);
      } else {
        next.delete(index);
      }
      return next;
    });
  };
  return (
    <>
      <HeldLines
        item={item}
        {...(selectable
          ? {
              selected: ticked,
              onSelect: (index: number, chosen: boolean) => {
                setUnticked((before) => {
                  const next = new Set(before);
                  if (chosen) {
                    next.delete(index);
                  } else {
                    next.add(index);
                  }
                  return next;
                });
              },
            }
          : {})}
        {...(correcting
          ? {
              drafts,
              onDraft: (index: number, draft: LineDraft | null) => {
                setDrafts((before) => {
                  const next = new Map(before);
                  if (draft === null) {
                    next.delete(index);
                  } else {
                    next.set(index, draft);
                  }
                  return next;
                });
                if (draft !== null) {
                  // A line being corrected is not one to leave out.
                  setLeaving(index, false);
                }
              },
              left,
              onLeave: (index: number, chosen: boolean) => {
                setLeaving(index, chosen);
                if (chosen) {
                  setDrafts((before) => {
                    const next = new Map(before);
                    next.delete(index);
                    return next;
                  });
                }
              },
            }
          : {})}
      />
      {selectable ? (
        <p className="ledger-muted">
          Untick a line to leave it out: only the ticked lines are imported,
          and PerFin does not ask about the others again.
        </p>
      ) : null}
      {asRead ? <p className="ledger-muted">{asReadNote(item)}</p> : null}
      {correcting ? (
        <>
          <p className="ledger-muted">
            “Correct” lets you type the right date, amount, quantity or kind of
            a line. Only the lines you corrected are imported. Tick “Leave out”
            for every other line: it is not imported, and PerFin does not ask
            about it again.
          </p>
          {fixes.length > 0 && undecided > 0 ? (
            <p role="status">
              {undecided === 1
                ? "1 line is neither corrected nor left out. Correct it, or tick “Leave out” for it."
                : `${String(undecided)} lines are neither corrected nor left out. Correct them, or tick “Leave out” for them.`}
            </p>
          ) : null}
        </>
      ) : null}
      <Refusal error={error} />
      <Actions>
        {asRead ? (
          <Button
            variant="primary"
            loading={resolve.isPending}
            disabled={selectable && ticked.size === 0}
            onClick={() => {
              resolve.mutate(
                someLines
                  ? importAsReadResolution([...ticked].sort((a, b) => a - b))
                  : importAsReadResolution(),
              );
            }}
          >
            {someLines
              ? `Import ${String(ticked.size)} of ${String(listed.length)} lines`
              : "Import as read"}
          </Button>
        ) : correcting ? (
          <Button
            variant="primary"
            loading={resolve.isPending}
            disabled={fixes.length === 0 || invalid || undecided > 0}
            onClick={() => {
              resolve.mutate(
                lineFixResolution(
                  fixes.map((fix) => fix.line).sort((a, b) => a - b),
                  fixes,
                ),
              );
            }}
          >
            Import the corrected lines
          </Button>
        ) : itemReason(item) === "investment_low_confidence" &&
          item.document_id !== null ? (
          <MapColumnsLink documentId={item.document_id} />
        ) : null}
        {trailing}
      </Actions>
    </>
  );
}

/** What a section shows besides its words when it is not the one to act on. */
function ItemInfo({
  item,
  actionable,
}: {
  item: ReviewItem;
  actionable: boolean;
}) {
  const reason = itemReason(item);
  if (isHeldStatement(item)) {
    return <HeldLines item={item} />;
  }
  if (
    item.kind === "possible_duplicate" &&
    reason !== "near_matches" &&
    reason !== "same_content_skipped"
  ) {
    return <DuplicateBody item={item} chosen={null} />;
  }
  if (
    item.kind === "low_confidence_field" &&
    reason === "investment_opening_basis_unknown"
  ) {
    return <CostBasisTools item={item} actionable={actionable} />;
  }
  if (item.kind === "new_institution") {
    return <NewInstitutionTools item={item} actionable={actionable} />;
  }
  return null;
}

type Control =
  | "account"
  | "owner"
  | "sign"
  | "map"
  | "duplicate"
  | "near"
  | "reconcile"
  | "rows"
  | "receipt"
  | "teach"
  | "reprocess"
  | "ok"
  | "held";

/**
 * The control of the card's action for the item that asks for it. An action
 * the item cannot take (the server and this page disagree) gives none: the
 * section then only explains and can be dismissed.
 */
function controlOf(
  action: CardAction,
  item: ReviewItem,
  previewable: boolean,
): Control | null {
  if (item.state !== "open") {
    return null;
  }
  if (isHeldStatement(item) && itemReason(item) !== "investment_figures_differ") {
    // Held lines are answered where they are listed, whatever the card says.
    return "held";
  }
  switch (action) {
    case "choose_account":
      return item.kind === "unknown_account" ? "account" : null;
    case "choose_owner":
      return item.kind === "unknown_owner" ? "owner" : null;
    case "answer_sign":
      return item.kind === "unparsed" ? "sign" : null;
    case "map_columns":
      return item.document_id !== null ? "map" : null;
    case "confirm_duplicate":
      if (item.kind !== "possible_duplicate") {
        return null;
      }
      return itemReason(item) === "near_matches" ? "near" : "duplicate";
    case "reconcile":
      return item.document_id !== null ? "reconcile" : null;
    case "check_rows":
      return item.kind === "low_confidence_field" && listsTransactions(item)
        ? "rows"
        : null;
    case "attach_receipt":
      return item.kind === "receipt_match" ? "receipt" : null;
    case "teach":
      return item.document_id !== null && previewable ? "teach" : null;
    case "reprocess":
      return item.kind === "unparsed" ? "reprocess" : null;
    case "acknowledge":
      return "ok";
    default:
      return null;
  }
}

/** One finding: its title and words, and the card's control when it is the one to act on. */
function Section({
  item,
  full,
  control,
  onReconcile,
  onTeach,
}: {
  item: ReviewItem;
  full: boolean;
  control: Control | null;
  onReconcile: () => void;
  onTeach: () => void;
}) {
  const explanation = explain(item);
  const actionable = item.state === "open" && item.can_resolve;
  const [error, setError] = useState<string | null>(null);
  const dismiss = useDismiss(item, setError);
  const trailing = actionable ? (
    <Button
      variant="ghost"
      loading={dismiss.isPending}
      onClick={() => {
        dismiss.mutate();
      }}
    >
      Dismiss
    </Button>
  ) : null;
  const props: ControlProps = { item, actionable, trailing };
  let body: ReactNode;
  switch (control) {
    case "account":
      body = <AccountControl {...props} />;
      break;
    case "owner":
      body = <UnknownOwnerTools {...props} />;
      break;
    case "sign":
      body = <SignControl {...props} />;
      break;
    case "map":
      body =
        actionable && item.document_id !== null ? (
          <Actions>
            <MapColumnsLink documentId={item.document_id} />
            {trailing}
          </Actions>
        ) : null;
      break;
    case "duplicate":
      body = <DuplicateControl {...props} />;
      break;
    case "near":
      body = <NearMatchesControl {...props} />;
      break;
    case "reconcile":
      // A viewer may look at the figures too; the view itself offers no action then.
      body = (
        <Actions>
          <Button variant="primary" onClick={onReconcile}>
            Check against the statement
          </Button>
          {trailing}
        </Actions>
      );
      break;
    case "rows":
      body = <LowConfidenceTools {...props} />;
      break;
    case "receipt":
      body = <ReceiptControl {...props} />;
      break;
    case "teach":
      body = actionable ? (
        <Actions>
          <Button variant="primary" onClick={onTeach}>
            Teach PerFin
          </Button>
          {trailing}
        </Actions>
      ) : null;
      break;
    case "reprocess":
      body = <ReprocessControl {...props} />;
      break;
    case "held":
      body = <HeldControl {...props} />;
      break;
    case "ok":
      body = (
        <>
          <ItemInfo item={item} actionable={actionable} />
          <AcknowledgeControl {...props} />
        </>
      );
      break;
    default:
      body = (
        <>
          <ItemInfo item={item} actionable={actionable} />
          {trailing === null ? null : <Actions>{trailing}</Actions>}
        </>
      );
  }
  return (
    <article className="review-section" aria-label={explanation.title}>
      <h3 className="review-section__title">{explanation.title}</h3>
      <ExplanationBlock explanation={explanation} full={full} />
      {body}
      <Refusal error={error} />
    </article>
  );
}

// ---- the card ------------------------------------------------------------------

/**
 * The reconcile view for a card: the statement its finding names (the view
 * chooses the one in doubt when it names none), with the boxes of the lines
 * the reader skipped, which the document's `lines_skipped` finding carries.
 */
function CardReconcile({
  documentId,
  items,
  asked,
  onDone,
  onTeach,
}: {
  documentId: string;
  items: readonly ReviewItem[];
  asked: ReviewItem | undefined;
  onDone: () => void;
  onTeach?: () => void;
}) {
  const skipped = items.find(
    (item) =>
      item.kind === "low_confidence_field" &&
      itemReason(item) === "lines_skipped",
  );
  const detail = useQuery({
    queryKey: reviewKeys.one(skipped?.id ?? ""),
    queryFn: () => getReviewItem(skipped?.id ?? ""),
    enabled: skipped !== undefined,
  });
  const regions = (detail.data?.regions ?? []).filter(
    (region) => region.label === "skipped_line",
  );
  return (
    <ReconcileView
      documentId={documentId}
      statement={asked === undefined ? null : payloadNumber(asked, "statement")}
      accountId={asked === undefined ? null : payloadString(asked, "account_id")}
      regions={regions}
      onDone={onDone}
      {...(onTeach === undefined ? {} : { onTeach })}
    />
  );
}

interface CardModel {
  document: ReviewDocument | null;
  items: readonly ReviewItem[];
  action: CardAction;
  actionItemId: string;
  /** Items that wait on a person (the others are notices). */
  open: number;
}

function CardView({ model }: { model: CardModel }) {
  const queryClient = useQueryClient();
  const [fixing, setFixing] = useState(false);
  const [teaching, setTeaching] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [shown, setShown] = useState(false);
  const document = model.document;
  const previewable = document?.previewable === true;
  const asked = model.items.find((item) => item.id === model.actionItemId);
  const control =
    asked === undefined ? null : controlOf(model.action, asked, previewable);
  const canAct = model.items.some(
    (item) => item.state === "open" && item.can_resolve,
  );
  const heading =
    model.open > 0
      ? `${String(model.open)} thing(s) to settle`
      : "For your information";
  const sections = (full: boolean) =>
    model.items.map((item) => (
      <Section
        key={item.id}
        item={item}
        full={full}
        control={item.id === model.actionItemId ? control : null}
        onReconcile={() => {
          setFixing(false);
          setReconciling(true);
        }}
        onTeach={() => {
          setFixing(false);
          setTeaching(true);
        }}
      />
    ));
  // Several cards share a heading ("1 thing(s) to settle"), so each region
  // is named after its file as well: landmarks need distinct names.
  const regionName = `${heading}: ${
    document?.original_filename ?? (asked === undefined ? "no file" : explain(asked).title)
  }`;
  return (
    <Card
      className="review-card"
      title={heading}
      aria-labelledby={undefined}
      aria-label={regionName}
      actions={
        <Button
          size="sm"
          onClick={() => {
            setFixing(true);
          }}
        >
          {canAct ? "Fix…" : "Details"}
        </Button>
      }
    >
      <div className="review-item">
        <DocumentLine document={document} />
        {fixing ? null : (
          <>
            {sections(false)}
            {document !== null && previewable ? (
              <div className="ledger-stack">
                <Actions>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setShown(!shown);
                    }}
                  >
                    {shown ? "Hide document" : "Show document"}
                  </Button>
                  {canAct && control !== "teach" ? (
                    <Button
                      size="sm"
                      onClick={() => {
                        setTeaching(true);
                      }}
                    >
                      Teach PerFin
                    </Button>
                  ) : null}
                </Actions>
                {shown ? <PageViewer documentId={document.id} /> : null}
              </div>
            ) : null}
          </>
        )}
      </div>
      {teaching && document !== null ? (
        <TeachDialog
          documentId={document.id}
          item={asked ?? null}
          readings={currentReadings(document, asked ?? null)}
          onClose={() => {
            setTeaching(false);
          }}
        />
      ) : null}
      {reconciling && document !== null ? (
        <Dialog
          open={reconciling}
          onClose={() => {
            setReconciling(false);
          }}
          title={document.original_filename ?? "Statement"}
          size="lg"
        >
          <CardReconcile
            documentId={document.id}
            items={model.items}
            asked={asked}
            {...(canAct && previewable
              ? {
                  onTeach: () => {
                    setReconciling(false);
                    setTeaching(true);
                  },
                }
              : {})}
            onDone={() => {
              setReconciling(false);
              void queryClient.invalidateQueries({ queryKey: queryRoots.review });
              void queryClient.invalidateQueries({
                queryKey: queryRoots.documents,
              });
              void queryClient.invalidateQueries({
                queryKey: queryRoots.transactions,
              });
            }}
          />
        </Dialog>
      ) : null}
      {fixing ? (
        <Dialog
          open={fixing}
          onClose={() => {
            setFixing(false);
          }}
          title={heading}
          size="lg"
        >
          <div className="review-fix">
            {document !== null && previewable ? (
              <div className="review-fix__document">
                <PageViewer documentId={document.id} />
              </div>
            ) : null}
            <div className="review-fix__steps ledger-stack">
              <DocumentLine document={document} />
              {sections(true)}
            </div>
          </div>
        </Dialog>
      ) : null}
    </Card>
  );
}

/** A document's open findings and the one thing to do next. */
export function DocumentCard({ card }: { card: ReviewCard }) {
  return (
    <CardView
      model={{
        document: card.document,
        items: card.items,
        action: card.action,
        actionItemId: card.action_item_id,
        open: card.open,
      }}
    />
  );
}

const LOOSE_ACTIONS: ReadonlyMap<ReviewItem["kind"], CardAction> = new Map([
  ["unknown_owner", "choose_owner"],
  ["unknown_account", "choose_account"],
  ["receipt_match", "attach_receipt"],
  ["possible_duplicate", "confirm_duplicate"],
]);

/** An open item of no document (a rejected email): a card of its own. */
export function LooseCard({ item }: { item: ReviewItem }) {
  return (
    <CardView
      model={{
        document: item.document ?? null,
        items: [item],
        action: LOOSE_ACTIONS.get(item.kind) ?? "acknowledge",
        actionItemId: item.id,
        open: 1,
      }}
    />
  );
}

/** An item that was resolved or dismissed, as it is kept in the queue's history. */
export function ClosedItem({ item }: { item: ReviewItem }) {
  const explanation = explain(item);
  return (
    <Card
      className="review-card"
      title={explanation.title}
      actions={
        <Badge tone={item.state === "resolved" ? "success" : "neutral"}>
          {item.state === "resolved" ? "Resolved" : "Dismissed"}
        </Badge>
      }
    >
      <div className="review-item">
        <DocumentLine document={item.document} />
        <ExplanationBlock explanation={explanation} full={false} />
      </div>
    </Card>
  );
}

// ---- after the last answer -----------------------------------------------------

/** "Settled.", with how many rows of the file are in which account. */
export function settledText(imported: DocumentImported | undefined): string {
  if (imported === undefined) {
    return "Settled.";
  }
  const names: string[] = [];
  let rows = 0;
  for (const statement of imported.statements) {
    if (statement.transactions > 0) {
      rows += statement.transactions;
      if (!names.includes(statement.account_name)) {
        names.push(statement.account_name);
      }
    }
  }
  if (rows > 0) {
    return `Settled. ${String(rows)} row(s) are in ${names.join(", ")}.`;
  }
  const counted = imported.transactions + imported.investment_transactions;
  return counted > 0
    ? `Settled. ${String(counted)} row(s) are in PerFin.`
    : "Settled. No rows from this file are in PerFin.";
}

/** Where a document's rows went, with the way to them and to the file. */
export function SettledLine({ document }: { document: ReviewDocument }) {
  const imported = useQuery({
    queryKey: docKeys.imported(document.id),
    queryFn: () => getImported(document.id),
  });
  return (
    <div className="review-settled">
      <p role="status">{settledText(imported.data)}</p>
      <Actions>
        <Link
          to="/transactions"
          search={{ document: document.id }}
          className="link-action"
        >
          View the rows
        </Link>
        <Link
          to="/documents/$documentId"
          params={{ documentId: document.id }}
          className="link-action"
        >
          Open the file
        </Link>
      </Actions>
    </div>
  );
}

/** A document nothing waits on any more: it stays on the page until it is left. */
export function SettledCard({ document }: { document: ReviewDocument }) {
  return (
    <Card className="review-card review-card--settled">
      <div className="review-item">
        <DocumentLine document={document} />
        <SettledLine document={document} />
      </div>
    </Card>
  );
}
