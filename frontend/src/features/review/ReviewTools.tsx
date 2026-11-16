// SPDX-License-Identifier: AGPL-3.0-or-later
/** The review tools: page highlights, alternative readings, remember layout, owner, held lines. */
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage, isApiError } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { useAccounts, accountLabel } from "../accounts/api";
import { useAuthSession } from "../auth/session";
import { PageViewer } from "../documents/PageViewer";
import { updateInvestmentTxn } from "../investments/api";
import { listMembers, settingsKeys } from "../settings/api";
import { getTransaction, txKeys } from "../transactions/api";
import {
  draftOf,
  fixFromAlternative,
  fixFromDraft,
  getReviewItem,
  institutionChoices,
  layoutRequest,
  lineFixable,
  lowConfidenceResolution,
  mergeFix,
  ownerResolution,
  payloadLines,
  payloadPositions,
  payloadString,
  payloadTransactionIds,
  rememberLayout,
  resolveItem,
  reviewKeys,
  type Alternative,
  type HeldLine,
  type LineDraft,
  type ResolveBody,
  type ReviewItem,
  type TransactionFix,
} from "./api";
import { keptText } from "./teach";

/** The item with its regions and alternatives (the list omits them). */
export function useItemDetail(item: ReviewItem, enabled = true) {
  return useQuery({
    queryKey: reviewKeys.one(item.id),
    queryFn: () => getReviewItem(item.id),
    enabled,
  });
}

/** The page highlights of an item, or nothing when it has no document. */
export function ItemViewer({
  item,
  detail,
}: {
  item: ReviewItem;
  detail: ReviewItem | undefined;
}) {
  if (item.document_id === null) {
    return null;
  }
  return (
    <PageViewer documentId={item.document_id} regions={detail?.regions ?? []} />
  );
}

/**
 * The ledger rows an item is about: the ones its payload lists, then any
 * other its detail names. A row is listed whether or not the detail has a
 * box for it: the server gives a box only while the stored reading still
 * equals the ledger row, so a row corrected after the import has none and
 * still waits.
 */
function rowIds(item: ReviewItem, detail: ReviewItem | undefined): string[] {
  const ids = new Set<string>(payloadTransactionIds(item));
  for (const region of detail?.regions ?? []) {
    if (region.transaction_id !== null && region.transaction_id !== undefined) {
      ids.add(region.transaction_id);
    }
  }
  for (const alt of detail?.alternatives ?? []) {
    if (alt.transaction_id !== null && alt.transaction_id !== undefined) {
      ids.add(alt.transaction_id);
    }
  }
  return [...ids];
}

/** The row is gone from the ledger (deleted, or no longer the caller's to see). */
function isGone(error: unknown): boolean {
  return isApiError(error) && error.status === 404;
}

/**
 * Resolve the item and reload what it may have changed. No toast: the card
 * says what happened once the list is back.
 */
export function useResolve(
  item: ReviewItem,
  onError: (message: string | null) => void,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ResolveBody) => resolveItem(item.id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
      onError(null);
    },
    onError: (caught) => {
      onError(errorMessage(caught));
    },
  });
}

function AlternativeButtons({
  alternatives,
  onPick,
}: {
  alternatives: readonly Alternative[];
  onPick: (alt: Alternative) => void;
}) {
  if (alternatives.length === 0) {
    return null;
  }
  return (
    <ul className="alternatives" aria-label="Other readings">
      {alternatives.map((alt) => (
        <li key={`${alt.field}-${alt.value}`}>
          <Button
            size="sm"
            onClick={() => {
              onPick(alt);
            }}
          >
            {`Use ${alt.field === "amount" ? "amount" : "date"} ${alt.value}`}
          </Button>
        </li>
      ))}
    </ul>
  );
}

function DoubtfulRow({
  id,
  alternatives,
  fix,
  onFix,
}: {
  id: string;
  alternatives: readonly Alternative[];
  fix: TransactionFix | undefined;
  onFix: (fix: TransactionFix) => void;
}) {
  const row = useQuery({
    queryKey: txKeys.one(id),
    queryFn: () => getTransaction(id),
  });
  if (row.isPending) {
    return <Spinner label="Loading row" />;
  }
  if (row.isError) {
    return <p className="ledger-muted">A row is no longer available.</p>;
  }
  const tx = row.data;
  return (
    <div
      className="ledger-stack"
      role="group"
      aria-label={`Row ${tx.raw_payee ?? tx.id}`}
    >
      <p>
        {fix?.posted_date ?? tx.posted_date} {tx.raw_payee ?? "-"}{" "}
        <MoneyText
          amount={
            fix?.amount === undefined || fix.amount === null
              ? tx.amount.amount
              : String(fix.amount.amount)
          }
          currency={tx.amount.currency}
        />
        {fix === undefined ? "" : " (corrected)"}
      </p>
      <AlternativeButtons
        alternatives={alternatives}
        onPick={(alt) => {
          const made = fixFromAlternative(alt, tx);
          if (made !== null) {
            onFix(made);
          }
        }}
      />
    </div>
  );
}

/**
 * Doubtful rows: highlighted on the page, with other readings to pick from.
 * Rendered for an item that lists ledger rows; "Confirm as read" is offered
 * while one of them still waits (``review_state`` ``pending``), whether or
 * not the page has a box for it. ``trailing``: the section's other buttons,
 * shown in the same row.
 */
export function LowConfidenceTools({
  item,
  actionable,
  trailing,
}: {
  item: ReviewItem;
  actionable: boolean;
  trailing?: ReactNode;
}) {
  const detail = useItemDetail(item);
  const ids = rowIds(item, detail.data);
  const rows = useQueries({
    queries: ids.map((id) => ({
      queryKey: txKeys.one(id),
      queryFn: () => getTransaction(id),
    })),
  });
  const [fixes, setFixes] = useState<TransactionFix[]>([]);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  const actions =
    trailing === undefined || trailing === null ? null : (
      <div className="ledger-actions">{trailing}</div>
    );
  if (detail.isPending) {
    return (
      <>
        <Spinner label="Loading the page" />
        {actions}
      </>
    );
  }
  if (detail.isError) {
    return (
      <>
        <p role="alert" className="ledger-error">
          {errorMessage(detail.error)}
        </p>
        {actions}
      </>
    );
  }
  if (rows.some((row) => row.isPending)) {
    return (
      <>
        <Spinner label="Loading the rows" />
        {actions}
      </>
    );
  }
  // A row waits when the ledger says so. One that could not be read (other
  // than because it is gone) may still wait: the server decides on confirm.
  const waiting = rows.some(
    (row) =>
      row.data?.review_state === "pending" ||
      (row.isError && !isGone(row.error)),
  );
  if (!waiting) {
    // Every row of the item is confirmed or gone: nothing to confirm or correct.
    return actions;
  }
  return (
    <div className="ledger-stack">
      <p>
        PerFin was not sure it read these rows correctly. Check them against the
        page.
      </p>
      <ItemViewer item={item} detail={detail.data} />
      {ids.map((id) => (
        <DoubtfulRow
          key={id}
          id={id}
          alternatives={(detail.data.alternatives ?? []).filter(
            (alt) => alt.transaction_id === id,
          )}
          fix={fixes.find((f) => f.transaction_id === id)}
          onFix={(fix) => {
            setFixes((previous) => mergeFix(previous, fix));
          }}
        />
      ))}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
      {actionable ? (
        <div className="ledger-actions">
          <Button
            variant="primary"
            loading={resolve.isPending}
            onClick={() => {
              resolve.mutate(lowConfidenceResolution(fixes, true));
            }}
          >
            {fixes.length === 0
              ? "Confirm as read"
              : "Save corrections and confirm"}
          </Button>
          {trailing}
        </div>
      ) : (
        actions
      )}
    </div>
  );
}

/** A new institution: remember the layout so the next statement of it is read the same way. */
export function NewInstitutionTools({
  item,
  actionable,
}: {
  item: ReviewItem;
  actionable: boolean;
}) {
  const queryClient = useQueryClient();
  const detail = useItemDetail(item);
  const [institution, setInstitution] = useState("");
  const [similar, setSimilar] = useState(true);
  const [replace, setReplace] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const remember = useMutation({
    mutationFn: () =>
      rememberLayout(item.id, layoutRequest(institution, similar, replace)),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
    },
    onError: (caught) => {
      setConflict(isApiError(caught) && caught.slug === "layout-conflict");
      setError(errorMessage(caught));
    },
  });
  if (detail.isPending) {
    return <Spinner label="Loading the page" />;
  }
  if (detail.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(detail.error)}
      </p>
    );
  }
  const choices = institutionChoices(detail.data);
  const out = remember.data;
  return (
    <div className="ledger-stack">
      <p>
        PerFin does not know this layout yet. Tell it which institution the file
        is from and it reads the next one the same way.
      </p>
      <ItemViewer item={item} detail={detail.data} />
      {actionable ? (
        <>
          <Field label="Institution">
            <Select
              placeholder="Choose an institution"
              options={choices.map((choice) => ({
                value: choice.id,
                label: choice.label,
              }))}
              value={institution}
              onChange={(event) => {
                setInstitution(event.target.value);
              }}
            />
          </Field>
          <Checkbox
            label="Read files already received with this layout again"
            checked={similar}
            onChange={(event) => {
              setSimilar(event.target.checked);
            }}
          />
          {conflict || replace ? (
            <Checkbox
              label="Replace the layout remembered for this institution (what was taught for it before is no longer used)"
              checked={replace}
              onChange={(event) => {
                setReplace(event.target.checked);
              }}
            />
          ) : null}
          <div className="ledger-actions">
            <Button
              variant="primary"
              disabled={institution === ""}
              loading={remember.isPending}
              onClick={() => {
                remember.mutate();
              }}
            >
              Remember this layout
            </Button>
          </div>
        </>
      ) : null}
      {out === undefined ? null : (
        <p role="status">
          Layout remembered. {out.reprocessed.length} file(s) are being read
          again.
          {(out.kept ?? []).length > 0
            ? ` What was taught for it before is kept: ${keptText(out.kept ?? [])}.`
            : ""}
          {out.ambiguous
            ? " The layout also matches other institutions, so check the next import."
            : ""}
        </p>
      )}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** A drop-folder file nobody could be found for: choose the member (and account). */
export function UnknownOwnerTools({
  item,
  actionable,
  trailing,
}: {
  item: ReviewItem;
  actionable: boolean;
  trailing?: ReactNode;
}) {
  const { session } = useAuthSession();
  const householdId = session?.household?.id ?? "";
  const members = useQuery({
    queryKey: settingsKeys.members(householdId),
    queryFn: () => listMembers(householdId),
    enabled: householdId !== "" && actionable,
  });
  const accounts = useAccounts();
  const [userId, setUserId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const resolve = useResolve(item, setError);
  return (
    <div className="ledger-stack">
      <p>
        A file arrived in the drop folder and PerFin could not tell whose it is.
        It is held until you choose.
      </p>
      {item.document_id === null ? null : (
        <Link
          to="/documents/$documentId"
          params={{ documentId: item.document_id }}
          className="link-action"
        >
          Look at the file
        </Link>
      )}
      {actionable ? (
        <>
          <Field label="Whose file is it?">
            <Select
              placeholder="Choose a member"
              options={(members.data ?? [])
                .filter((m) => m.is_active)
                .map((m) => ({ value: m.user_id, label: m.display_name }))}
              value={userId}
              onChange={(event) => {
                setUserId(event.target.value);
              }}
            />
          </Field>
          <Field label="Account (optional)">
            <Select
              placeholder="Let PerFin match the account"
              options={(accounts.data ?? [])
                .filter((a) => a.can_edit && !a.is_closed)
                .map((a) => ({ value: a.id, label: accountLabel(a) }))}
              value={accountId}
              onChange={(event) => {
                setAccountId(event.target.value);
              }}
            />
          </Field>
          <Checkbox
            label="Send files from this subfolder to this member from now on"
            checked={remember}
            onChange={(event) => {
              setRemember(event.target.checked);
            }}
          />
          <div className="ledger-actions">
            <Button
              variant="primary"
              disabled={userId === ""}
              loading={resolve.isPending}
              onClick={() => {
                resolve.mutate(ownerResolution(userId, accountId, remember));
              }}
            >
              Assign this file
            </Button>
            {trailing}
          </div>
        </>
      ) : null}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** The receipt itself, shown on request next to the suggested transactions. */
export function ReceiptPreview({ item }: { item: ReviewItem }) {
  const [shown, setShown] = useState(false);
  if (item.document_id === null) {
    return null;
  }
  return (
    <div className="ledger-stack">
      <Button
        size="sm"
        onClick={() => {
          setShown(!shown);
        }}
      >
        {shown ? "Hide receipt" : "Show receipt"}
      </Button>
      {shown ? <PageViewer documentId={item.document_id} /> : null}
    </div>
  );
}

/** An opening position whose cost basis is unknown (``investment_opening_basis_unknown``). */
interface OpeningPosition {
  transactionId: string;
  quantity: string;
  tradeDate: string;
}

export function openingPositions(item: ReviewItem): OpeningPosition[] {
  const value: unknown = new Map(Object.entries(item.payload)).get("securities");
  if (!Array.isArray(value)) {
    return [];
  }
  const out: OpeningPosition[] = [];
  for (const entry of value as unknown[]) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const fields = new Map<string, unknown>(Object.entries(entry));
    const id = fields.get("transaction_id");
    if (typeof id !== "string") {
      continue;
    }
    const quantity = fields.get("quantity");
    const day = fields.get("trade_date");
    out.push({
      transactionId: id,
      quantity: typeof quantity === "string" ? quantity : "",
      tradeDate: typeof day === "string" ? day : "",
    });
  }
  return out;
}

function CostBasisRow({ position }: { position: OpeningPosition }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [price, setPrice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => updateInvestmentTxn(position.transactionId, { price: price.trim() }),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryRoots.investments });
      toast.notify({ title: "Cost basis saved", tone: "success" });
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  const text = price.trim();
  const valid = text !== "" && Number.isFinite(Number(text)) && Number(text) >= 0;
  return (
    <div className="ledger-stack" role="group" aria-label={`Opening position of ${position.quantity} units`}>
      <p>
        {position.quantity} units held before {formatDate(position.tradeDate) ?? position.tradeDate}
      </p>
      <Field label="Cost per unit" hint="What one unit cost when it was bought.">
        <Input
          inputMode="decimal"
          value={price}
          onChange={(event) => {
            setPrice(event.target.value);
          }}
        />
      </Field>
      <div className="ledger-actions">
        <Button
          size="sm"
          disabled={!valid}
          loading={save.isPending}
          onClick={() => {
            save.mutate();
          }}
        >
          Save cost basis
        </Button>
        {save.isSuccess ? <span role="status">Saved.</span> : null}
      </div>
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </div>
  );
}

/** Enter the cost basis of opening positions (``PATCH /investments/transactions/{id}``). */
export function CostBasisTools({ item, actionable }: { item: ReviewItem; actionable: boolean }) {
  const positions = openingPositions(item);
  if (!actionable || positions.length === 0) {
    return null;
  }
  return (
    <div className="ledger-stack">
      {positions.map((position) => (
        <CostBasisRow key={position.transactionId} position={position} />
      ))}
      <p className="ledger-muted">Dismiss this item once every basis is entered.</p>
    </div>
  );
}

const DOUBT_WORDS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    reconciliation: "its section does not add up",
    suggested_type: "reading suggested by the local model",
    held_with_security: "waits with its holding",
    market_value: "quantity × price is not the value printed",
    trade_date: "date outside the statement's period",
    amount: "amount hard to read",
    security: "security not identified",
    quantity: "quantity not read",
    type: "kind of activity unclear",
  }),
);

const KIND_WORDS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    buy: "Purchase",
    sell: "Sale",
    dividend: "Dividend",
    interest: "Interest",
    fee: "Fee",
    reinvest: "Reinvestment",
    transfer_in: "Shares in",
    transfer_out: "Shares out",
    deposit: "Deposit",
    withdrawal: "Withdrawal",
    corporate_action: "Corporate action",
    split: "Split",
  }),
);

function doubtWords(fields: string[]): string {
  return fields.map((f) => DOUBT_WORDS.get(f) ?? f).join("; ");
}

function lineName(line: HeldLine): string {
  const day =
    line.date === null ? "this row" : (formatDate(line.date) ?? line.date);
  return `${day}${line.description === null ? "" : `, ${line.description}`}`;
}

const KIND_OPTIONS = [...KIND_WORDS].map(([value, label]) => ({ value, label }));

/**
 * The values of one held line, to type over: its date, amount, quantity and
 * kind. What is left as read is not sent (``fixFromDraft``).
 */
function LineEditor({
  line,
  draft,
  currency,
  onChange,
  onClose,
}: {
  line: HeldLine;
  draft: LineDraft;
  currency: string | null;
  onChange: (draft: LineDraft) => void;
  onClose: () => void;
}) {
  const invalid = fixFromDraft(line, draft, currency) === "invalid";
  return (
    <div
      className="review-line-editor"
      role="group"
      aria-label={`Correct the line of ${lineName(line)}`}
    >
      <Field label="Date">
        <Input
          type="date"
          value={draft.date}
          onChange={(event) => {
            onChange({ ...draft, date: event.target.value });
          }}
        />
      </Field>
      {currency === null ? null : (
        <>
          <Field label="Amount" hint="Money out is negative.">
            <Input
              inputMode="decimal"
              value={draft.amount}
              onChange={(event) => {
                onChange({ ...draft, amount: event.target.value });
              }}
            />
          </Field>
          <Field label="Quantity" hint="Leave empty to keep what was read.">
            <Input
              inputMode="decimal"
              value={draft.quantity}
              onChange={(event) => {
                onChange({ ...draft, quantity: event.target.value });
              }}
            />
          </Field>
        </>
      )}
      <Field label="Kind">
        <Select
          placeholder="As read"
          options={KIND_OPTIONS}
          value={draft.type}
          onChange={(event) => {
            onChange({ ...draft, type: event.target.value });
          }}
        />
      </Field>
      <div className="ledger-actions">
        <Button size="sm" variant="ghost" onClick={onClose}>
          Leave as read
        </Button>
      </div>
      {invalid ? (
        <p role="alert" className="ledger-error">
          Enter the date as a day and the amounts as plain numbers.
        </p>
      ) : null}
    </div>
  );
}

/**
 * The activity lines of a brokerage statement that are in doubt (payload
 * ``lines``): the date, section, description and amount printed, the page
 * to find each on, and why PerFin doubts it. Nothing here is in the ledger
 * yet; the item's actions decide about all of them, or, where the lines
 * stand alone (``selected`` and ``onSelect`` given), about the ticked ones.
 * With ``drafts`` and ``onDraft``, a line whose only doubts are its date,
 * amount, quantity or kind gets "Correct": the values are typed over and the
 * card imports the corrected lines. With ``left`` and ``onLeave`` too, every
 * line has a "Leave out" tick: importing the corrected lines drops the others
 * for good, so the person says so line by line. The holdings that wait with
 * them (payload ``positions``) are listed under the lines.
 */
export function HeldLines({
  item,
  selected,
  onSelect,
  drafts,
  onDraft,
  left,
  onLeave,
}: {
  item: ReviewItem;
  selected?: ReadonlySet<number>;
  onSelect?: (index: number, chosen: boolean) => void;
  drafts?: ReadonlyMap<number, LineDraft>;
  onDraft?: (index: number, draft: LineDraft | null) => void;
  left?: ReadonlySet<number>;
  onLeave?: (index: number, chosen: boolean) => void;
}) {
  const lines = payloadLines(item);
  const positions = payloadPositions(item);
  if (lines.length === 0 && positions.length === 0) {
    return null;
  }
  const currency = payloadString(item, "currency");
  const choosing = selected !== undefined && onSelect !== undefined;
  const correcting =
    drafts !== undefined && onDraft !== undefined && lines.some(lineFixable);
  const leaving = correcting && left !== undefined && onLeave !== undefined;
  return (
    <>
      {lines.length === 0 ? null : (
        <div
          className="mapping-grid"
          role="region"
          aria-label="Lines in doubt"
          tabIndex={0}
        >
          <table className="table table--compact">
            <caption className="visually-hidden">Lines in doubt</caption>
            <thead>
              <tr>
                {choosing ? <th scope="col">Import</th> : null}
                <th scope="col">Date</th>
                <th scope="col">Section</th>
                <th scope="col">Description</th>
                <th scope="col">Read as</th>
                <th scope="col">Amount</th>
                <th scope="col">Page</th>
                <th scope="col">Why</th>
                {correcting ? (
                  <th scope="col">
                    <span className="visually-hidden">Correct</span>
                  </th>
                ) : null}
                {leaving ? <th scope="col">Leave out</th> : null}
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.index}>
                  {choosing ? (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Import the line of ${lineName(line)}`}
                        checked={selected.has(line.index)}
                        onChange={(event) => {
                          onSelect(line.index, event.target.checked);
                        }}
                      />
                    </td>
                  ) : null}
                  <td>
                    {line.date === null
                      ? ""
                      : (formatDate(line.date) ?? line.date)}
                  </td>
                  <td>{line.section ?? ""}</td>
                  <td>{line.description ?? ""}</td>
                  <td>
                    {line.type === null
                      ? ""
                      : (KIND_WORDS.get(line.type) ?? line.type)}
                  </td>
                  <td>
                    {line.amount === null ? (
                      ""
                    ) : currency === null ? (
                      line.amount
                    ) : (
                      <MoneyText amount={line.amount} currency={currency} />
                    )}
                  </td>
                  <td>{line.page === null ? "" : String(line.page)}</td>
                  <td>{doubtWords(line.fields)}</td>
                  {correcting ? (
                    <td>
                      {lineFixable(line) ? (
                        <Button
                          size="sm"
                          aria-expanded={drafts.has(line.index)}
                          onClick={() => {
                            if (!drafts.has(line.index)) {
                              onDraft(line.index, draftOf(line));
                            }
                          }}
                        >
                          Correct
                        </Button>
                      ) : null}
                    </td>
                  ) : null}
                  {leaving ? (
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`Leave out the line of ${lineName(line)}`}
                        checked={left.has(line.index)}
                        onChange={(event) => {
                          onLeave(line.index, event.target.checked);
                        }}
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {correcting
        ? lines.map((line) => {
            const draft = drafts.get(line.index);
            return draft === undefined ? null : (
              <LineEditor
                key={line.index}
                line={line}
                draft={draft}
                currency={currency}
                onChange={(next) => {
                  onDraft(line.index, next);
                }}
                onClose={() => {
                  onDraft(line.index, null);
                }}
              />
            );
          })
        : null}
      {positions.length === 0 ? null : (
        <div
          className="mapping-grid"
          role="region"
          aria-label="Holdings waiting"
          tabIndex={0}
        >
          <table className="table table--compact">
            <caption className="visually-hidden">Holdings waiting</caption>
            <thead>
              <tr>
                <th scope="col">Holding</th>
                <th scope="col">Symbol</th>
                <th scope="col">Page</th>
                <th scope="col">Why</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => (
                <tr key={position.index}>
                  <td>{position.name ?? ""}</td>
                  <td>{position.symbol ?? ""}</td>
                  <td>{position.page === null ? "" : String(position.page)}</td>
                  <td>
                    {position.fields.length === 0
                      ? "waits with its lines"
                      : doubtWords(position.fields)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
