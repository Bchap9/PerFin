// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Check a statement against the rows it produced: the page on the left, the
 * figures, the likely causes of a difference and the rows on the right.
 * Three exits: confirm (only when it balances), accept as read, finish later.
 */
import "../accounts/ledger.css";
import "./review.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";

import { Button } from "../../components/Button";
import { cx } from "../../components/cx";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { formatDate, isIsoDate } from "../../lib/dates";
import { negateAmount } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import type { Region } from "../documents/api";
import { PageViewer } from "../documents/PageViewer";
import {
  createTransaction,
  deleteTransaction,
  invalidateLedger,
  newIdempotencyKey,
  updateTransaction,
} from "../transactions/api";
import {
  acceptReconciliation,
  confirmReconciliation,
  getReconciliation,
  removeEarlierRows,
  restoreRow,
  reviewKeys,
  type ReconcileCause,
  type ReconcileRow,
  type StatementReconciliation,
} from "./api";
import {
  causeText,
  expectedClosing,
  liveDifference,
  readPrinted,
  skippedText,
  statementToCheck,
  statusText,
  teachHint,
} from "./reconcile";

const ACCEPT_QUESTION =
  "Accept this statement as read? Its rows are posted and it is marked “accepted without balancing”. No balance is recorded.";
const CARD_HINT =
  "Enter the balances as printed. For a card, the amount owed is a positive number.";
const NOT_AN_AMOUNT = "Enter the amount as a number. Money out is negative.";
const NOT_A_DATE = "Enter the date of the row.";

export interface ReconcileViewProps {
  documentId: string;
  /**
   * The statement's index among the document's statements. `null`: the
   * caller does not know which; the first statement still in doubt is shown
   * (`statementToCheck`), of `accountId` when that is given.
   */
  statement: number | null;
  /** Called after a confirmation or an acceptance, and for "Finish later". */
  onDone: () => void;
  /** More boxes to draw on the page (the lines a reader skipped). */
  regions?: readonly Region[];
  /** The account the caller's finding names (used when `statement` is `null`). */
  accountId?: string | null;
  /** Opens "Teach PerFin" for the document; offered beside the teach hint. */
  onTeach?: () => void;
}

export function ReconcileView({
  documentId,
  statement,
  onDone,
  regions = [],
  accountId = null,
  onTeach,
}: ReconcileViewProps) {
  const query = useQuery({
    queryKey: reviewKeys.reconciliation(documentId),
    queryFn: () => getReconciliation(documentId),
  });
  if (query.isPending) {
    return <Spinner label="Loading the statement" />;
  }
  const found = query.isError
    ? undefined
    : statementToCheck(query.data.statements, statement, accountId);
  if (found === undefined) {
    return (
      <div className="reconcile">
        <p role="alert" className="ledger-error">
          {query.isError
            ? errorMessage(query.error)
            : "This statement is not available to you."}
        </p>
        <div className="ledger-actions">
          <Button onClick={onDone}>Close</Button>
        </div>
      </div>
    );
  }
  return (
    <Statement
      documentId={documentId}
      statement={found}
      onDone={onDone}
      regions={regions}
      {...(onTeach === undefined ? {} : { onTeach })}
    />
  );
}

function dateText(iso: string | null): string {
  return iso === null ? "" : (formatDate(iso) ?? iso);
}

function rowName(row: ReconcileRow): string {
  return row.payee ?? dateText(row.posted_date);
}

function rowRegion(row: ReconcileRow): Region | null {
  return row.page === null || row.bbox === null
    ? null
    : { page: row.page, bbox: row.bbox, label: "transaction" };
}

function causeRegion(cause: ReconcileCause): Region | null {
  return cause.page === null || cause.bbox === null
    ? null
    : { page: cause.page, bbox: cause.bbox, label: "transaction" };
}

function rowNotes(row: ReconcileRow): string[] {
  const notes: string[] = [];
  if (row.origin === "matched") {
    notes.push("First came from another file");
  } else if (row.origin === "added") {
    notes.push("Added by hand");
  }
  if (row.edited) {
    notes.push("Changed after import");
  }
  if (!row.in_period) {
    notes.push("Outside the statement period");
  }
  return notes;
}

interface StatementProps {
  documentId: string;
  statement: StatementReconciliation;
  onDone: () => void;
  regions: readonly Region[];
  onTeach?: () => void;
}

interface Shown {
  region: Region;
  /** The row the box belongs to (none: a line no row was read from). */
  rowId: string | null;
}

interface RowDraft {
  id: string;
  date: string;
  amount: string;
}

interface NewRow {
  date: string;
  amount: string;
  description: string;
}

const EMPTY_ROW: NewRow = { date: "", amount: "", description: "" };

function Statement({ documentId, statement, onDone, regions, onTeach }: StatementProps) {
  const queryClient = useQueryClient();
  const headingId = useId();
  const rowsCaptionId = useId();
  const { currency } = statement;
  const canEdit = statement.can_edit;
  const [opening, setOpening] = useState(statement.opening ?? "");
  const [closing, setClosing] = useState(statement.closing ?? "");
  const [shown, setShown] = useState<Shown | null>(null);
  const [edits, setEdits] = useState({ flipped: 0, removed: 0 });
  const [draft, setDraft] = useState<RowDraft | null>(null);
  const [adding, setAdding] = useState<NewRow | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const rowElements = useRef(new Map<string, HTMLTableRowElement>());

  /** A change to the rows: afterwards the sum is read again. */
  const change = useMutation({
    mutationFn: (job: () => Promise<unknown>) => job(),
    onSuccess: async () => {
      invalidateLedger(queryClient);
      await queryClient.invalidateQueries({
        queryKey: reviewKeys.reconciliation(documentId),
      });
    },
  });
  /** An exit: the statement is settled, the lists it was on are stale. */
  const exit = useMutation({
    mutationFn: (job: () => Promise<unknown>) => job(),
    onSuccess: () => {
      invalidateLedger(queryClient);
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      setAsking(false);
      onDone();
    },
  });
  const busy = change.isPending || exit.isPending;
  const failure = change.isError
    ? errorMessage(change.error)
    : exit.isError
      ? errorMessage(exit.error)
      : null;

  const live = liveDifference(opening, closing, statement.total, statement.printed_sign);
  const expected = expectedClosing(opening, statement.total, statement.printed_sign);
  const flagged = new Set(statement.causes.flatMap((cause) => cause.transaction_ids));
  const hasEarlier = statement.causes.some((cause) => cause.kind === "earlier_reading");
  const hint = teachHint(edits);
  const skipped = skippedText(
    statement.lines_skipped,
    regions.some((region) => region.label === "skipped_line"),
  );
  const period =
    statement.period_start !== null && statement.period_end !== null
      ? `${dateText(statement.period_start)} to ${dateText(statement.period_end)}`
      : dateText(statement.period_end ?? statement.period_start);

  const showCause = (cause: ReconcileCause): void => {
    const first = statement.rows.find((row) =>
      cause.transaction_ids.includes(row.transaction_id),
    );
    const region = causeRegion(cause) ?? (first === undefined ? null : rowRegion(first));
    setShown(
      region === null ? null : { region, rowId: first?.transaction_id ?? null },
    );
    const element =
      first === undefined ? undefined : rowElements.current.get(first.transaction_id);
    if (element !== undefined && typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "center" });
      element.focus();
    }
  };

  const saveDraft = (): void => {
    if (draft === null) {
      return;
    }
    const amount = readPrinted(draft.amount);
    if (amount === null) {
      setFormError(NOT_AN_AMOUNT);
      return;
    }
    if (!isIsoDate(draft.date)) {
      setFormError(NOT_A_DATE);
      return;
    }
    setFormError(null);
    const { id, date } = draft;
    change.mutate(async () => {
      await updateTransaction(id, {
        posted_date: date,
        amount: { amount, currency },
      });
      setDraft(null);
    });
  };

  const addRow = (): void => {
    if (adding === null) {
      return;
    }
    const amount = readPrinted(adding.amount);
    if (amount === null) {
      setFormError(NOT_AN_AMOUNT);
      return;
    }
    if (!isIsoDate(adding.date)) {
      setFormError(NOT_A_DATE);
      return;
    }
    setFormError(null);
    const description = adding.description.trim();
    const key = newIdempotencyKey();
    const { date } = adding;
    change.mutate(async () => {
      await createTransaction(
        {
          account_id: statement.account_id,
          posted_date: date,
          amount: { amount, currency },
          ...(description === "" ? {} : { raw_payee: description }),
          status: "cleared",
          source_document_id: documentId,
        },
        key,
      );
      setAdding(null);
    });
  };

  const pageRegions = shown === null ? regions : [shown.region, ...regions];

  return (
    <section className="reconcile" aria-labelledby={headingId}>
      <div>
        <h2 id={headingId} className="reconcile__title">
          Check against the statement
        </h2>
        <p className="ledger-muted">
          {period === "" ? statement.account_name : `${statement.account_name}, ${period}`}
        </p>
      </div>
      <div className="reconcile__panes">
        <div className="reconcile__pane">
          <PageViewer
            // The viewer opens on the page of the box it is given.
            key={shown === null ? "page" : `page-${String(shown.region.page)}`}
            documentId={documentId}
            regions={pageRegions}
          />
        </div>
        <div className="reconcile__pane">
          {statement.printed_sign === "owed" && canEdit ? (
            <p className="ledger-muted">{CARD_HINT}</p>
          ) : null}
          <fieldset className="reconcile__figures" aria-label="The statement's figures">
            {canEdit ? (
              <>
                <Field label="Opening balance">
                  <Input
                    inputMode="decimal"
                    autoComplete="off"
                    maxLength={32}
                    value={opening}
                    onChange={(event) => {
                      setOpening(event.target.value);
                    }}
                  />
                </Field>
                <Field label="Closing balance">
                  <Input
                    inputMode="decimal"
                    autoComplete="off"
                    maxLength={32}
                    value={closing}
                    onChange={(event) => {
                      setClosing(event.target.value);
                    }}
                  />
                </Field>
              </>
            ) : (
              <>
                <Figure label="Opening balance" amount={statement.opening} currency={currency} />
                <Figure label="Closing balance" amount={statement.closing} currency={currency} />
              </>
            )}
            <Figure label="Rows in PerFin" amount={statement.total} currency={currency} />
            <Figure label="Opening + rows" amount={expected} currency={currency} />
          </fieldset>
          <p
            className={cx("reconcile__difference", `reconcile__difference--${live.state}`)}
            aria-live="polite"
            data-testid="reconcile-difference"
          >
            <span>Difference</span>
            <span className="reconcile__difference-amount">
              {live.text === "" ? (
                "—"
              ) : (
                <MoneyText amount={live.text} currency={currency} tone="none" />
              )}
            </span>
            <span>{statusText(live.state, live.text, currency)}</span>
          </p>
          {skipped === null ? null : <p className="ledger-muted">{skipped}</p>}
          {statement.causes.length === 0 ? null : (
            <div>
              <h3>Likely causes</h3>
              <ol className="reconcile__causes">
                {statement.causes.map((cause, index) => {
                  const named = cause.transaction_ids.length > 0;
                  const onPage = causeRegion(cause) !== null;
                  return (
                    <li key={`${cause.kind}-${String(index)}`}>
                      <p>{causeText(cause, currency)}</p>
                      {named || onPage ? (
                        <Button
                          size="sm"
                          onClick={() => {
                            showCause(cause);
                          }}
                        >
                          {named
                            ? cause.transaction_ids.length === 1
                              ? "Show the row"
                              : "Show the rows"
                            : "Show the line on the page"}
                        </Button>
                      ) : null}
                    </li>
                  );
                })}
              </ol>
              {hasEarlier && canEdit ? (
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    change.mutate(() =>
                      removeEarlierRows(documentId, { statement: statement.statement }),
                    );
                  }}
                >
                  Remove the rows of the earlier reading
                </Button>
              ) : null}
            </div>
          )}
          {hint === null ? null : (
            <div className="ledger-stack">
              <p role="status">{hint}</p>
              {onTeach === undefined || !canEdit ? null : (
                <div className="ledger-actions">
                  <Button size="sm" onClick={onTeach}>
                    Teach PerFin
                  </Button>
                </div>
              )}
            </div>
          )}
          {failure === null ? null : (
            <p role="alert" className="ledger-error">
              {failure}
            </p>
          )}
          <div
            className="table-wrap table-wrap--md"
            role="region"
            aria-labelledby={rowsCaptionId}
            // A scrollable region must be reachable by keyboard (WCAG 2.1.1).
            tabIndex={0}
          >
            <table className="table table--compact">
              <caption id={rowsCaptionId} className="table__caption">
                Rows of this statement in PerFin
              </caption>
              <thead>
                <tr>
                  <th scope="col">Row</th>
                  <th scope="col">Date</th>
                  <th scope="col" className="align-end">
                    Amount
                  </th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {statement.rows.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="table__empty">
                      No rows of this statement are in PerFin.
                    </td>
                  </tr>
                ) : (
                  statement.rows.map((row) => {
                    const id = row.transaction_id;
                    const name = rowName(row);
                    const region = rowRegion(row);
                    const chosen = shown?.rowId === id;
                    const editing = draft?.id === id ? draft : null;
                    return (
                      <tr
                        key={id}
                        ref={(element) => {
                          if (element === null) {
                            rowElements.current.delete(id);
                          } else {
                            rowElements.current.set(id, element);
                          }
                        }}
                        tabIndex={-1}
                        className={cx(
                          flagged.has(id) && "reconcile__row--flagged",
                          chosen && "reconcile__row--chosen",
                        )}
                      >
                        <th scope="row" data-label="Row">
                          {name}
                          {flagged.has(id) ? (
                            <span className="reconcile__note reconcile__note--flag">
                              Check this row
                            </span>
                          ) : null}
                          {rowNotes(row).map((note) => (
                            <span key={note} className="reconcile__note">
                              {note}
                            </span>
                          ))}
                        </th>
                        <td data-label="Date">
                          {editing === null ? (
                            dateText(row.posted_date)
                          ) : (
                            <Input
                              type="date"
                              aria-label="Date of the row"
                              value={editing.date}
                              onChange={(event) => {
                                setDraft({ ...editing, date: event.target.value });
                              }}
                            />
                          )}
                        </td>
                        <td data-label="Amount" className="align-end">
                          {editing === null ? (
                            <MoneyText amount={row.amount} currency={currency} />
                          ) : (
                            <Input
                              inputMode="decimal"
                              autoComplete="off"
                              aria-label="Amount of the row"
                              value={editing.amount}
                              onChange={(event) => {
                                setDraft({ ...editing, amount: event.target.value });
                              }}
                            />
                          )}
                        </td>
                        <td>
                          <div className="reconcile__row-actions">
                            {region === null ? null : (
                              <Button
                                size="sm"
                                variant="ghost"
                                aria-pressed={chosen}
                                aria-label={`Show on the page: ${name}`}
                                onClick={() => {
                                  setShown(chosen ? null : { region, rowId: id });
                                }}
                              >
                                Show on the page
                              </Button>
                            )}
                            {!canEdit ? null : editing !== null ? (
                              <>
                                <Button size="sm" variant="primary" disabled={busy} onClick={saveDraft}>
                                  Save
                                </Button>
                                <Button
                                  size="sm"
                                  onClick={() => {
                                    setDraft(null);
                                    setFormError(null);
                                  }}
                                >
                                  Cancel
                                </Button>
                              </>
                            ) : (
                              <>
                                <Button
                                  size="sm"
                                  aria-label={`Edit: ${name}`}
                                  disabled={busy}
                                  onClick={() => {
                                    setFormError(null);
                                    setDraft({ id, date: row.posted_date, amount: row.amount });
                                  }}
                                >
                                  Edit
                                </Button>
                                <Button
                                  size="sm"
                                  aria-label={`Flip sign: ${name}`}
                                  disabled={busy}
                                  onClick={() => {
                                    change.mutate(async () => {
                                      await updateTransaction(id, {
                                        amount: { amount: negateAmount(row.amount), currency },
                                      });
                                      setEdits((now) => ({ ...now, flipped: now.flipped + 1 }));
                                    });
                                  }}
                                >
                                  Flip sign
                                </Button>
                                <Button
                                  size="sm"
                                  aria-label={`Remove: ${name}`}
                                  disabled={busy}
                                  onClick={() => {
                                    change.mutate(async () => {
                                      await deleteTransaction(id);
                                      setEdits((now) => ({ ...now, removed: now.removed + 1 }));
                                      setShown((now) => (now?.rowId === id ? null : now));
                                    });
                                  }}
                                >
                                  Remove
                                </Button>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
          {statement.rows_truncated ? (
            <p className="ledger-muted">
              Only the first rows are listed. The total counts every row.
            </p>
          ) : null}
          {formError === null ? null : (
            <p role="alert" className="ledger-error">
              {formError}
            </p>
          )}
          {!canEdit ? null : adding === null ? (
            <div className="ledger-actions">
              <Button
                size="sm"
                onClick={() => {
                  setFormError(null);
                  setAdding(EMPTY_ROW);
                }}
              >
                Add a row
              </Button>
            </div>
          ) : (
            <form
              className="reconcile__add"
              aria-label="A row the statement prints and PerFin did not read"
              onSubmit={(event) => {
                event.preventDefault();
                addRow();
              }}
            >
              <Field label="Date" required>
                <Input
                  type="date"
                  value={adding.date}
                  onChange={(event) => {
                    setAdding({ ...adding, date: event.target.value });
                  }}
                />
              </Field>
              <Field label="Amount" required hint="Money out is negative.">
                <Input
                  inputMode="decimal"
                  autoComplete="off"
                  maxLength={32}
                  value={adding.amount}
                  onChange={(event) => {
                    setAdding({ ...adding, amount: event.target.value });
                  }}
                />
              </Field>
              <Field label="Description">
                <Input
                  maxLength={200}
                  value={adding.description}
                  onChange={(event) => {
                    setAdding({ ...adding, description: event.target.value });
                  }}
                />
              </Field>
              <div className="ledger-actions">
                <Button type="submit" size="sm" variant="primary" disabled={busy}>
                  Add the row
                </Button>
                <Button
                  size="sm"
                  onClick={() => {
                    setAdding(null);
                    setFormError(null);
                  }}
                >
                  Cancel
                </Button>
              </div>
            </form>
          )}
          {statement.removed.length === 0 ? null : (
            <div>
              <h3>Removed rows</h3>
              <ul className="reconcile__removed" aria-label="Removed rows">
                {statement.removed.map((removed) => {
                  const when = dateText(removed.posted_date);
                  return (
                    <li key={removed.tombstone_id}>
                      <span>{when === "" ? "A removed row" : when}</span>
                      {removed.amount === null ? null : (
                        <MoneyText amount={removed.amount} currency={currency} />
                      )}
                      {canEdit ? (
                        <Button
                          size="sm"
                          aria-label={when === "" ? "Put back" : `Put back: ${when}`}
                          disabled={busy}
                          onClick={() => {
                            change.mutate(() =>
                              restoreRow(documentId, { tombstone_id: removed.tombstone_id }),
                            );
                          }}
                        >
                          Put back
                        </Button>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      </div>
      <div className="ledger-actions">
        {canEdit ? (
          <>
            <Button
              variant="primary"
              disabled={live.state !== "balanced" || busy}
              loading={exit.isPending && !asking}
              onClick={() => {
                exit.mutate(() =>
                  confirmReconciliation(documentId, {
                    statement: statement.statement,
                    opening_balance: opening.trim(),
                    closing_balance: closing.trim(),
                  }),
                );
              }}
            >
              Confirm and record the balance
            </Button>
            <Button onClick={onDone}>Finish later</Button>
            <Button
              disabled={live.state === "balanced" || busy}
              onClick={() => {
                setAsking(true);
              }}
            >
              Accept as read
            </Button>
          </>
        ) : (
          <Button onClick={onDone}>Close</Button>
        )}
      </div>
      {asking ? (
        <Dialog
          open
          size="sm"
          title="Accept as read"
          onClose={() => {
            setAsking(false);
          }}
          footer={
            <>
              <Button
                onClick={() => {
                  setAsking(false);
                }}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={exit.isPending}
                onClick={() => {
                  exit.mutate(() =>
                    acceptReconciliation(documentId, { statement: statement.statement }),
                  );
                }}
              >
                Accept as read
              </Button>
            </>
          }
        >
          <p>{ACCEPT_QUESTION}</p>
          {/* The dialog covers the view: a refusal is said where the person is. */}
          {exit.isError ? (
            <p role="alert" className="ledger-error">
              {errorMessage(exit.error)}
            </p>
          ) : null}
        </Dialog>
      ) : null}
    </section>
  );
}

function Figure({
  label,
  amount,
  currency,
}: {
  label: string;
  amount: string | null;
  currency: string;
}) {
  return (
    <p className="reconcile__figure">
      <span className="reconcile__figure-label">{label}</span>
      <span className="reconcile__figure-value">
        {amount === null ? (
          <>
            <span aria-hidden="true">—</span>
            <span className="visually-hidden">Not known</span>
          </>
        ) : (
          <MoneyText amount={amount} currency={currency} tone="none" />
        )}
      </span>
    </p>
  );
}
