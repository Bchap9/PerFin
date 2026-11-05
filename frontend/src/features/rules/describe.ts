// SPDX-License-Identifier: AGPL-3.0-or-later
import { CONDITION_TYPES, TEXT_OPS, type ConditionDraft, type GroupDraft } from "./builder";

export type Names = (kind: "payee" | "category" | "account" | "tag", id: string) => string;

function list(kind: Parameters<Names>[0], ids: readonly string[], names: Names): string {
  return ids.map((id) => names(kind, id)).join(", ");
}

/** One line of plain language for a condition. */
export function describeCondition(item: ConditionDraft, names: Names): string {
  const prefix = item.negate ? "not: " : "";
  const label = CONDITION_TYPES.find((type) => type.value === item.type)?.label ?? item.type;
  switch (item.type) {
    case "payee":
    case "memo":
      return `${prefix}${label} ${TEXT_OPS.find((op) => op.value === item.op)?.label ?? item.op} "${item.value}"`;
    case "payee_is":
      return `${prefix}Payee is ${list("payee", item.ids, names)}`;
    case "account":
      return `${prefix}Account is ${list("account", item.ids, names)}`;
    case "category_is":
      return `${prefix}Category is ${list("category", item.ids, names)}`;
    case "amount": {
      const parts = [item.min === "" ? null : `at least ${item.min}`, item.max === "" ? null : `at most ${item.max}`].filter((part) => part !== null);
      return `${prefix}${item.absolute ? "Absolute amount" : "Amount"} ${parts.join(" and ")}`;
    }
    case "sign":
      return `${prefix}${item.sign === "inflow" ? "Money in" : "Money out"}`;
    case "currency":
      return `${prefix}Currency ${item.currencies}`;
    case "date":
      return `${prefix}Date ${[item.from === "" ? null : `from ${item.from}`, item.to === "" ? null : `to ${item.to}`].filter((part) => part !== null).join(" ")}`;
    case "category_unset":
      return `${prefix}Has no category`;
  }
}

export function describeGroup(group: GroupDraft, names: Names): string {
  if (group.items.length === 0) {
    return "Every transaction";
  }
  return group.items.map((item) => describeCondition(item, names)).join(group.match === "all" ? " and " : " or ");
}

export interface ChangeSet {
  added_tag_ids?: string[];
  category_id?: { before: string | null; after: string | null } | null;
  notes?: { before: string | null; after: string | null } | null;
  payee_id?: { before: string | null; after: string | null } | null;
  review_state?: { before: string; after: string } | null;
  splits?: {
    before: { amount: { amount: string }; category_id: string | null }[];
    after: { amount: { amount: string }; category_id: string | null }[];
  } | null;
}

const NONE = "none";

/** What a dry run would change on one transaction, one short line per field. */
export function describeChanges(changes: ChangeSet, names: Names): string[] {
  const lines: string[] = [];
  const name = (kind: "payee" | "category", id: string | null | undefined): string => (id === null || id === undefined ? NONE : names(kind, id));
  if (changes.category_id) {
    lines.push(`Category: ${name("category", changes.category_id.before)} to ${name("category", changes.category_id.after)}`);
  }
  if (changes.payee_id) {
    lines.push(`Payee: ${name("payee", changes.payee_id.before)} to ${name("payee", changes.payee_id.after)}`);
  }
  if (changes.added_tag_ids && changes.added_tag_ids.length > 0) {
    lines.push(`Tags added: ${changes.added_tag_ids.map((id) => names("tag", id)).join(", ")}`);
  }
  if (changes.notes) {
    lines.push(`Note: ${changes.notes.before ?? NONE} to ${changes.notes.after ?? NONE}`);
  }
  if (changes.review_state) {
    lines.push(`Review: ${changes.review_state.before} to ${changes.review_state.after}`);
  }
  if (changes.splits) {
    const text = (parts: NonNullable<ChangeSet["splits"]>["after"]): string =>
      parts.length === 0 ? "not split" : parts.map((part) => `${part.amount.amount} ${name("category", part.category_id)}`).join(" + ");
    lines.push(`Split: ${text(changes.splits.before)} to ${text(changes.splits.after)}`);
  }
  return lines;
}
