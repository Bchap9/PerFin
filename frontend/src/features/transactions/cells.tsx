// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Cells of the transactions grid. A category is a chip that says who or what
 * decided it; a suggestion is a question with Accept, never a chip. The
 * merchant cell keeps the bank's own wording beside the name, and the row's
 * state, tags and note. Changing a category happens in the drawer or with
 * the bulk bar, not in the grid.
 */
import "./transactions.css";

import { useId, useState } from "react";

import { Button } from "../../components/Button";
import { Chip } from "../../components/Chip";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { categoryColourClass } from "../../lib/categoryColour";
import { merchantName, type Category, type Payee, type Tag, type Transaction } from "./api";
import { useAcceptSuggestion, useEditTransaction } from "./optimistic";
import { confidenceText, isGuess, isSuggestion, sourceLabel } from "./provenance";

export function rowLabel(row: Transaction, payees: readonly Payee[]): string {
  return payees.find((payee) => payee.id === row.payee_id)?.name ?? row.raw_payee ?? "transaction";
}

/** The small line under a category: who or what decided, and how sure a guess is. */
function Why({ row, ruleNames }: { row: Transaction; ruleNames: ReadonlyMap<string, string> | undefined }) {
  const suggestion = isSuggestion(row);
  // Until the rule names are there, a rule is not called missing.
  const ruleId = row.category_rule_id ?? null;
  const source =
    row.category_source === "rule" && ruleId !== null && ruleNames === undefined
      ? "Rule"
      : sourceLabel(row, ruleId === null ? undefined : ruleNames?.get(ruleId));
  const confidence = suggestion || isGuess(row.category_source) ? confidenceText(row.category_confidence) : null;
  if (!suggestion && source === null && confidence === null) {
    return null;
  }
  return (
    <span className="cat-cell__why">
      {suggestion ? <span>Suggestion</span> : null}
      {source === null ? null : <span>{source}</span>}
      {confidence === null ? null : <span>{confidence}</span>}
    </span>
  );
}

export function CategoryCell({
  row,
  editable,
  label,
  categories,
  ruleNames,
  onCategorised,
  categoriesFailed = false,
}: {
  row: Transaction;
  editable: boolean;
  label: string;
  categories: readonly Category[];
  ruleNames?: ReadonlyMap<string, string> | undefined;
  /** The server accepted the suggestion for this row (the category is the person's now). */
  onCategorised?: ((row: Transaction, categoryId: string) => void) | undefined;
  /** The category list could not be read, so a name may be missing for good. */
  categoriesFailed?: boolean;
}) {
  const accept = useAcceptSuggestion();
  if (row.splits.length > 0) {
    return (
      <span className="cat-cell">
        <Chip>Split ({row.splits.length})</Chip>
      </span>
    );
  }
  const why = <Why row={row} ruleNames={ruleNames} />;
  // A suggestion whose category is not in the list cannot be offered by name.
  const suggested = isSuggestion(row) ? categories.find((category) => category.id === row.suggested_category_id)?.name : undefined;
  if (suggested !== undefined) {
    return (
      <span className="cat-cell">
        <span className="cat-cell__choice">
          <span className="cat-cell__suggestion">{suggested}?</span>
          {editable ? (
            <Button
              size="sm"
              aria-label={`Accept the suggestion for ${label}`}
              loading={accept.isPending}
              onClick={() => {
                const accepted = row.suggested_category_id ?? "";
                accept.mutate(row.id, {
                  onSuccess: (result) => {
                    if (result.changed > 0 && accepted !== "") {
                      onCategorised?.(row, accepted);
                    }
                  },
                });
              }}
            >
              Accept
            </Button>
          ) : null}
        </span>
        {why}
      </span>
    );
  }
  const name = categories.find((category) => category.id === row.category_id)?.name;
  return (
    <span className="cat-cell">
      {row.category_id === null ? (
        <span className="cat-cell__none">Uncategorised</span>
      ) : name === undefined ? (
        // The names are still loading, or failed to: never call a categorised row uncategorised.
        <span className="cat-cell__none">{categoriesFailed ? "Category not loaded" : "Category…"}</span>
      ) : (
        <Chip colourClass={categoryColourClass(row.category_id, categories)}>{name}</Chip>
      )}
      {why}
    </span>
  );
}

const same = (a: string | null | undefined, b: string | null | undefined): boolean =>
  (a ?? "").trim().toLowerCase() === (b ?? "").trim().toLowerCase();

/** The name a row goes by: its payee, else the merchant PerFin recognised, else the bank's wording. */
export function displayName(row: Transaction, payees: readonly Payee[]): string | null {
  const payee = payees.find((item) => item.id === row.payee_id)?.name ?? null;
  const raw = (row.raw_payee ?? "").trim() === "" ? null : row.raw_payee;
  return payee ?? merchantName(row) ?? raw;
}

/**
 * The merchant: the payee, else the merchant PerFin recognised, else the
 * bank's wording. Under it the recognised merchant when it says more, the
 * bank's own wording whenever it differs, the row's state, its tags and its
 * note.
 */
export function MerchantCell({ row, payees, tags }: { row: Transaction; payees: readonly Payee[]; tags: readonly Tag[] }) {
  const raw = (row.raw_payee ?? "").trim() === "" ? null : row.raw_payee;
  const recognised = merchantName(row);
  const name = displayName(row, payees);
  const merchant = recognised !== null && name !== recognised && !same(recognised, raw) ? recognised : null;
  const tagNames = row.tag_ids.map((id) => tags.find((tag) => tag.id === id)?.name).filter((tag): tag is string => tag !== undefined);
  const notes = (row.notes ?? "").trim();
  const pending = row.status === "pending";
  const review = row.review_state === "pending";
  const transfer = row.transfer !== null;
  return (
    <span className="tx-merchant">
      <span className="tx-merchant__name">{name ?? "-"}</span>
      {merchant === null ? null : <span className="tx-merchant__recognised">{merchant}</span>}
      {raw === null || same(raw, name) ? null : <span className="tx-merchant__raw">{raw}</span>}
      {pending || review || transfer ? (
        <span className="tx-merchant__marks">
          {pending ? <Chip tone="warn">Pending</Chip> : null}
          {review ? <Chip tone="info">Needs review</Chip> : null}
          {transfer ? <Chip>Transfer</Chip> : null}
        </span>
      ) : null}
      {tagNames.length === 0 ? null : (
        <span className="tx-merchant__tags">
          <span className="visually-hidden">Tags: </span>
          {tagNames.map((tag) => (
            <span key={tag} className="tx-merchant__tag">
              {tag}
            </span>
          ))}
        </span>
      )}
      {notes === "" ? null : (
        <span className="tx-merchant__note">
          <span className="visually-hidden">Note: </span>
          {notes}
        </span>
      )}
    </span>
  );
}

/** The note of one row, edited in a small dialog from the row menu. Saved optimistically, rolled back on failure. */
export function NoteDialog({
  row,
  label,
  onClose,
  edit: given,
}: {
  row: Transaction;
  label: string;
  onClose: () => void;
  /** The caller's edit, so the caller can say a refusal where it is seen (the drawer is modal, over the toasts). */
  edit?: ReturnType<typeof useEditTransaction>;
}) {
  const own = useEditTransaction();
  const edit = given ?? own;
  const formId = useId();
  const [draft, setDraft] = useState(row.notes ?? "");
  const save = (): void => {
    const value = draft.trim();
    if (value !== (row.notes ?? "")) {
      edit.mutate({ id: row.id, patch: { notes: value === "" ? null : value } });
    }
    onClose();
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Note on ${label}`}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form={formId}>
            Save note
          </Button>
        </>
      }
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <Field label="Note">
          <Input
            value={draft}
            maxLength={2000}
            onChange={(event) => {
              setDraft(event.target.value);
            }}
          />
        </Field>
      </form>
    </Dialog>
  );
}
