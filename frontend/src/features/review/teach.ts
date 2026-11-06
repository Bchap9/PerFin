// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Teach PerFin": the state of the teach dialog and the request it sends
 * (`POST /documents/{id}/teach`, docs/INGESTION.md "Teaching PerFin a
 * layout"). Pure functions so the choices and the body are testable.
 *
 * Every picker offers an escape: "Other — describe it" (a free-text note the
 * household can read; PerFin never acts on it) and "Ignore this".
 */
import type { components } from "../../lib/api/schema";
import { wordInside } from "../documents/highlight";
import { ACTIVITY_ROLES, COLUMN_ROLES } from "../imports/mapping";
import type { TeachRequest } from "./api";

type Schemas = components["schemas"];
export type TeachField = Schemas["TeachLabel"]["field"];
export type TeachRole = Schemas["TeachColumn"]["role"];
export type TableKind = Schemas["TeachTable"]["kind"];
/** What a selection is: a field, a table, "ignore" or "other". */
export type SelectionKind = TeachField | TableKind;

export interface Choice<T extends string> {
  value: T;
  label: string;
}

export const OTHER_LABEL = "Other — describe it";
export const IGNORE_LABEL = "Ignore this";

/** What a selection can be; always ends with "Other" and "Ignore". */
export const SELECTION_CHOICES: readonly Choice<SelectionKind>[] = [
  { value: "account_number", label: "Account number" },
  { value: "account_name", label: "Account name / type" },
  { value: "period_start", label: "Statement start date" },
  { value: "period_end", label: "Statement end date" },
  { value: "opening_balance", label: "Opening balance" },
  { value: "closing_balance", label: "Closing balance" },
  { value: "institution", label: "Institution" },
  { value: "transactions", label: "Transaction table" },
  { value: "holdings", label: "Holdings table" },
  { value: "other", label: OTHER_LABEL },
  { value: "ignore", label: IGNORE_LABEL },
];

export function selectionLabel(kind: SelectionKind | ""): string {
  return SELECTION_CHOICES.find((c) => c.value === kind)?.label ?? "Not chosen yet";
}

export function isTable(kind: SelectionKind | ""): kind is TableKind {
  return kind === "transactions" || kind === "holdings";
}

const TRANSACTION_ROLE_VALUES: readonly string[] = [
  "date",
  "payee",
  "memo",
  "amount",
  "debit",
  "credit",
  "balance",
];
const HOLDING_ROLE_VALUES: readonly string[] = ["symbol", "security", "quantity", "price"];

const ESCAPES: readonly Choice<TeachRole>[] = [
  { value: "other", label: OTHER_LABEL },
  { value: "ignore", label: IGNORE_LABEL },
];

/** Column roles of a labelled table (the mapping editor's), then "Other" and "Ignore". */
export function rolesForTable(kind: TableKind): readonly Choice<TeachRole>[] {
  if (kind === "transactions") {
    return [
      ...COLUMN_ROLES.filter((r) => TRANSACTION_ROLE_VALUES.includes(r.value)).map(
        (r) => ({ value: r.value as TeachRole, label: r.label }),
      ),
      ...ESCAPES,
    ];
  }
  return [
    ...ACTIVITY_ROLES.filter((r) => HOLDING_ROLE_VALUES.includes(r.value)).map((r) => ({
      value: r.value as TeachRole,
      label: r.label,
    })),
    { value: "market_value", label: "Market value" },
    { value: "cost_basis", label: "Cost basis" },
    ...ESCAPES,
  ];
}

/** A word box of the page (``GET /documents/{id}/regions``). */
export interface PageWord {
  bbox: readonly number[];
  line: number;
  text?: string | null;
}

export interface ColumnDraft {
  x0: number;
  x1: number;
  header: string;
  role: TeachRole | "";
  note: string;
}

export interface Selection {
  id: string;
  page: number;
  bbox: [number, number, number, number];
  /** The words inside the selection, as the page reads. */
  text: string;
  kind: SelectionKind | "";
  /** Free text for "Other — describe it". */
  note: string;
  /** The value as the person reads it (optional correction). */
  value: string;
  columns: ColumnDraft[];
}

export function wordsIn(words: readonly PageWord[], bbox: readonly number[]): PageWord[] {
  return words.filter((w) => wordInside(w.bbox, bbox));
}

function textOf(words: readonly PageWord[]): string {
  return [...words]
    .sort((a, b) => a.line - b.line || (a.bbox[0] ?? 0) - (b.bbox[0] ?? 0))
    .map((w) => w.text ?? "")
    .filter((t) => t !== "")
    .join(" ");
}

/** The word under a point (page units), or null. */
export function wordAt(words: readonly PageWord[], x: number, y: number): PageWord | null {
  return (
    words.find((w) => {
      const [x0, y0, x1, y1] = w.bbox;
      return (
        x0 !== undefined &&
        y0 !== undefined &&
        x1 !== undefined &&
        y1 !== undefined &&
        x >= x0 &&
        x <= x1 &&
        y >= y0 &&
        y <= y1
      );
    }) ?? null
  );
}

/** One box around ``words`` (null when there are none). */
export function boxAround(
  words: readonly PageWord[],
): [number, number, number, number] | null {
  if (words.length === 0) {
    return null;
  }
  return [
    Math.min(...words.map((w) => w.bbox[0] ?? 0)),
    Math.min(...words.map((w) => w.bbox[1] ?? 0)),
    Math.max(...words.map((w) => w.bbox[2] ?? 0)),
    Math.max(...words.map((w) => w.bbox[3] ?? 0)),
  ];
}

/** The page's lines (for choosing one with the keyboard): text and box. */
export function pageLines(
  words: readonly PageWord[],
): { line: number; text: string; bbox: [number, number, number, number] }[] {
  const byLine = new Map<number, PageWord[]>();
  for (const word of words) {
    const list = byLine.get(word.line) ?? [];
    list.push(word);
    byLine.set(word.line, list);
  }
  return [...byLine.entries()]
    .sort(([a], [b]) => a - b)
    .flatMap(([line, list]) => {
      const bbox = boxAround(list);
      const text = textOf(list);
      return bbox === null || text === "" ? [] : [{ line, text, bbox }];
    });
}

/**
 * Columns of a selected table from its first line (the header): header
 * words closer than about two word heights are one column; each column
 * reaches halfway to its neighbours and the outer ones to the box's edges.
 */
export function columnsFromWords(
  words: readonly PageWord[],
  bbox: readonly number[],
): ColumnDraft[] {
  const inside = wordsIn(words, bbox);
  if (inside.length === 0) {
    return [];
  }
  const first = Math.min(...inside.map((w) => w.line));
  const header = inside
    .filter((w) => w.line === first)
    .sort((a, b) => (a.bbox[0] ?? 0) - (b.bbox[0] ?? 0));
  const groups: PageWord[][] = [];
  for (const word of header) {
    const last = groups[groups.length - 1];
    const previous = last?.[last.length - 1];
    const height = Math.max(1, (word.bbox[3] ?? 0) - (word.bbox[1] ?? 0));
    if (previous !== undefined && (word.bbox[0] ?? 0) - (previous.bbox[2] ?? 0) <= height * 1.2) {
      last?.push(word);
    } else {
      groups.push([word]);
    }
  }
  const left = bbox[0] ?? 0;
  const right = bbox[2] ?? 0;
  return groups.map((group, index) => {
    const start = Math.min(...group.map((w) => w.bbox[0] ?? 0));
    const end = Math.max(...group.map((w) => w.bbox[2] ?? 0));
    const before = groups[index - 1];
    const after = groups[index + 1];
    const x0 =
      before === undefined
        ? left
        : (Math.max(...before.map((w) => w.bbox[2] ?? 0)) + start) / 2;
    const x1 =
      after === undefined ? right : (end + Math.min(...after.map((w) => w.bbox[0] ?? 0))) / 2;
    return { x0, x1, header: textOf(group), role: "", note: "" };
  });
}

let counter = 0;

/** A new selection of ``bbox`` on ``page`` (kind not chosen yet). */
export function newSelection(
  page: number,
  bbox: [number, number, number, number],
  words: readonly PageWord[],
): Selection {
  counter += 1;
  return {
    id: `sel-${String(counter)}`,
    page,
    bbox,
    text: textOf(wordsIn(words, bbox)),
    kind: "",
    note: "",
    value: "",
    columns: columnsFromWords(words, bbox),
  };
}

/** What is still missing before the selections can be sent (empty: ready). */
export function problems(selections: readonly Selection[]): string[] {
  const out: string[] = [];
  for (const [index, sel] of selections.entries()) {
    const name = `Selection ${String(index + 1)}`;
    if (sel.kind === "") {
      out.push(`${name}: say what it is.`);
    } else if (sel.kind === "other" && sel.note.trim() === "") {
      out.push(`${name}: describe what it is.`);
    } else if (isTable(sel.kind)) {
      if (sel.columns.length < 2) {
        out.push(`${name}: a table needs at least two columns.`);
      }
      for (const [at, column] of sel.columns.entries()) {
        if (column.role === "") {
          out.push(`${name}: choose the role of column ${String(at + 1)}.`);
        } else if (column.role === "other" && column.note.trim() === "") {
          out.push(`${name}: describe column ${String(at + 1)}.`);
        }
      }
    }
  }
  return out;
}

export interface TeachOptions {
  institutionName: string;
  remember: boolean;
  reprocessSimilar: boolean;
  itemId: string | null;
  /** Start the remembered layout afresh instead of adding to what was taught. */
  replace?: boolean;
}

/** The request body for the selections (selections without a kind are left out). */
export function teachBody(selections: readonly Selection[], options: TeachOptions): TeachRequest {
  const labels: NonNullable<TeachRequest["labels"]> = [];
  const tables: NonNullable<TeachRequest["tables"]> = [];
  for (const sel of selections) {
    if (sel.kind === "") {
      continue;
    }
    if (isTable(sel.kind)) {
      tables.push({
        kind: sel.kind,
        page: sel.page,
        bbox: [...sel.bbox],
        columns: sel.columns.map((c) => ({
          x0: c.x0,
          x1: c.x1,
          role: c.role === "" ? "ignore" : c.role,
          ...(c.role === "other" ? { note: c.note.trim() } : {}),
        })),
      });
      continue;
    }
    labels.push({
      field: sel.kind,
      page: sel.page,
      bbox: [...sel.bbox],
      ...(sel.kind === "other" ? { note: sel.note.trim() } : {}),
      ...(sel.value.trim() !== "" && sel.kind !== "other" && sel.kind !== "ignore"
        ? { value: sel.value.trim() }
        : {}),
    });
  }
  const name = options.institutionName.trim();
  return {
    labels,
    tables,
    remember: options.remember,
    reprocess_similar: options.reprocessSimilar,
    replace: options.remember && options.replace === true,
    resolve: options.itemId !== null,
    ...(name === "" ? {} : { institution_name: name }),
    ...(options.itemId === null ? {} : { item_id: options.itemId }),
  };
}

/** Plain words for the server's warning codes. */
export function warningText(code: string): string {
  const [head, field] = code.split(":");
  const named = field === undefined ? "" : selectionLabel(field as SelectionKind).toLowerCase();
  switch (head) {
    case "no_label":
      return `No label is printed next to the ${named}, so it is used for this file only.`;
    case "unreadable":
      return `The ${named} could not be read from the selection; type it in.`;
    case "rows_already_imported":
      return "Rows of this file were imported before; the table is remembered but not imported again.";
    case "does_not_reconcile":
      return "With these balances the statement still does not add up.";
    case "ignore_not_rememberable":
      return "A line to ignore needs some words without figures to be remembered.";
    case "table_without_rows":
      return "No row could be read from the table with these columns.";
    case "template_field_missing":
      return `The ${named} was not found again on the page.`;
    case "item_left_open":
      return "The review item stays open: what was taught does not settle it yet.";
    case "note_without_words":
      return "A note held only figures, so it was not kept (notes keep no numbers).";
    default:
      return code;
  }
}

/** Plain words for what a remembered layout kept from earlier teaching. */
export function keptText(kept: readonly string[]): string {
  return kept
    .map((code) => {
      if (code === "table:transactions") {
        return "the transaction table";
      }
      if (code === "table:holdings") {
        return "the holdings table";
      }
      return selectionLabel(code as SelectionKind).toLowerCase();
    })
    .join(", ");
}
