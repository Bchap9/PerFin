// SPDX-License-Identifier: AGPL-3.0-or-later
/** Inline-edit cells of the transactions grid. Each commit goes through `useEditTransaction` (optimistic, rolled back on failure). */
import "./transactions.css";

import { useState } from "react";

import { Button } from "../../components/Button";
import { CategoryPicker } from "../../components/CategoryPicker";
import { Checkbox } from "../../components/Checkbox";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import type { Category, Payee, Tag, Transaction } from "./api";
import { useAcceptSuggestion, useEditTransaction } from "./optimistic";
import { confidenceText, isGuess, isSuggestion, sourceLabel } from "./provenance";

export function rowLabel(row: Transaction, payees: readonly Payee[]): string {
  return payees.find((payee) => payee.id === row.payee_id)?.name ?? row.raw_payee ?? "transaction";
}

interface CellProps {
  row: Transaction;
  editable: boolean;
  label: string;
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
}: CellProps & { categories: readonly Category[]; ruleNames?: ReadonlyMap<string, string> | undefined }) {
  const edit = useEditTransaction();
  const accept = useAcceptSuggestion();
  if (row.splits.length > 0) {
    return <span>Split ({row.splits.length})</span>;
  }
  const name = categories.find((category) => category.id === row.category_id)?.name;
  // A suggestion whose category is not in the list cannot be offered by name.
  const suggested = isSuggestion(row) ? categories.find((category) => category.id === row.suggested_category_id)?.name : undefined;
  const why = <Why row={row} ruleNames={ruleNames} />;
  const picker = (placeholder?: string) => (
    <span className="cell-edit">
      <CategoryPicker
        aria-label={`Category of ${label}`}
        categories={categories}
        {...(placeholder === undefined ? {} : { placeholder })}
        value={row.category_id ?? ""}
        onChange={(event) => {
          edit.mutate({ id: row.id, patch: { category_id: event.target.value === "" ? null : event.target.value } });
        }}
      />
    </span>
  );
  if (suggested !== undefined) {
    return (
      <span className="cat-cell">
        <span className="cat-cell__suggestion">{suggested}?</span>
        {editable ? (
          <span className="cat-cell__choice">
            <Button
              size="sm"
              aria-label={`Accept the suggestion for ${label}`}
              loading={accept.isPending}
              onClick={() => {
                accept.mutate(row.id);
              }}
            >
              Accept
            </Button>
            {picker("Choose another")}
          </span>
        ) : null}
        {why}
      </span>
    );
  }
  return (
    <span className="cat-cell">
      {editable ? picker() : <span>{name ?? "Uncategorised"}</span>}
      {why}
    </span>
  );
}

const same = (a: string | null | undefined, b: string | null | undefined): boolean =>
  (a ?? "").trim().toLowerCase() === (b ?? "").trim().toLowerCase();

/**
 * The payee, the merchant PerFin recognised (when that says more than the
 * payee or the descriptor already do) and, always, the bank's own wording.
 */
export function PayeeCell({ row, editable, label, payees }: CellProps & { payees: readonly Payee[] }) {
  const edit = useEditTransaction();
  const payee = payees.find((item) => item.id === row.payee_id)?.name;
  const raw = (row.raw_payee ?? "").trim() === "" ? null : row.raw_payee;
  const merchant = (row.merchant ?? "").trim() === "" || same(row.merchant, raw) || same(row.merchant, payee) ? null : row.merchant;
  // Read-only, a row without a payee is headed by its merchant or else its descriptor, said once.
  const heading = payee ?? (merchant === null ? raw : null);
  return (
    <span className="payee-cell">
      {editable ? (
        <span className="cell-edit">
          <Select
            aria-label={`Payee of ${label}`}
            placeholder="No payee"
            options={payees.map((item) => ({ value: item.id, label: item.name }))}
            value={row.payee_id ?? ""}
            onChange={(event) => {
              edit.mutate({ id: row.id, patch: { payee_id: event.target.value === "" ? null : event.target.value } });
            }}
          />
        </span>
      ) : heading === null ? null : (
        <span>{heading}</span>
      )}
      {merchant === null ? null : <span className="payee-cell__merchant">{merchant}</span>}
      {raw === null || (!editable && heading === raw) ? null : <span className="payee-cell__raw">{raw}</span>}
      {!editable && heading === null && merchant === null && raw === null ? <span>-</span> : null}
    </span>
  );
}

export function TagsCell({ row, editable, label, tags }: CellProps & { tags: readonly Tag[] }) {
  const edit = useEditTransaction();
  const names = row.tag_ids.map((id) => tags.find((tag) => tag.id === id)?.name).filter((name): name is string => name !== undefined);
  if (!editable) {
    return <span>{names.join(", ") || "-"}</span>;
  }
  return (
    <details className="cell-edit">
      <summary aria-label={`Tags of ${label}`}>{names.join(", ") || "Add tags"}</summary>
      <div className="tag-picker">
        {tags.map((tag) => (
          <Checkbox
            key={tag.id}
            label={tag.name}
            checked={row.tag_ids.includes(tag.id)}
            onChange={(event) => {
              const next = event.target.checked ? [...row.tag_ids, tag.id] : row.tag_ids.filter((id) => id !== tag.id);
              edit.mutate({ id: row.id, patch: { tag_ids: next } });
            }}
          />
        ))}
        {tags.length === 0 ? <span>No tags yet.</span> : null}
      </div>
    </details>
  );
}

export function NotesCell({ row, editable, label }: CellProps) {
  const edit = useEditTransaction();
  const [draft, setDraft] = useState<string | null>(null);
  if (!editable) {
    return <span>{row.notes ?? ""}</span>;
  }
  if (draft === null) {
    return (
      <button
        type="button"
        className="cell-text-button"
        aria-label={`Edit notes of ${label}`}
        onClick={() => {
          setDraft(row.notes ?? "");
        }}
      >
        {row.notes === null || row.notes === "" ? "Add note" : row.notes}
      </button>
    );
  }
  const commit = (): void => {
    const value = draft.trim();
    setDraft(null);
    if (value !== (row.notes ?? "")) {
      edit.mutate({ id: row.id, patch: { notes: value === "" ? null : value } });
    }
  };
  return (
    <Input
      autoFocus
      aria-label={`Notes of ${label}`}
      value={draft}
      maxLength={2000}
      onChange={(event) => {
        setDraft(event.target.value);
      }}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          commit();
        } else if (event.key === "Escape") {
          setDraft(null);
        }
      }}
    />
  );
}
