// SPDX-License-Identifier: AGPL-3.0-or-later
/** Inline-edit cells of the transactions grid. Each commit goes through `useEditTransaction` (optimistic, rolled back on failure). */
import { useState } from "react";

import { Checkbox } from "../../components/Checkbox";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import type { Category, Payee, Tag, Transaction } from "./api";
import { useEditTransaction } from "./optimistic";

export function rowLabel(row: Transaction, payees: readonly Payee[]): string {
  return payees.find((payee) => payee.id === row.payee_id)?.name ?? row.raw_payee ?? "transaction";
}

interface CellProps {
  row: Transaction;
  editable: boolean;
  label: string;
}

export function CategoryCell({ row, editable, label, categories }: CellProps & { categories: readonly Category[] }) {
  const edit = useEditTransaction();
  if (row.splits.length > 0) {
    return <span>Split ({row.splits.length})</span>;
  }
  const name = categories.find((category) => category.id === row.category_id)?.name;
  if (!editable) {
    return <span>{name ?? "Uncategorised"}</span>;
  }
  return (
    <span className="cell-edit">
      <Select
        aria-label={`Category of ${label}`}
        placeholder="Uncategorised"
        options={categories.map((category) => ({ value: category.id, label: category.name }))}
        value={row.category_id ?? ""}
        onChange={(event) => {
          edit.mutate({ id: row.id, patch: { category_id: event.target.value === "" ? null : event.target.value } });
        }}
      />
    </span>
  );
}

export function PayeeCell({ row, editable, label, payees }: CellProps & { payees: readonly Payee[] }) {
  const edit = useEditTransaction();
  const name = payees.find((payee) => payee.id === row.payee_id)?.name ?? row.raw_payee;
  if (!editable) {
    return <span>{name ?? "-"}</span>;
  }
  return (
    <span className="cell-edit">
      <Select
        aria-label={`Payee of ${label}`}
        placeholder={row.raw_payee ?? "No payee"}
        options={payees.map((payee) => ({ value: payee.id, label: payee.name }))}
        value={row.payee_id ?? ""}
        onChange={(event) => {
          edit.mutate({ id: row.id, patch: { payee_id: event.target.value === "" ? null : event.target.value } });
        }}
      />
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
