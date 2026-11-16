// SPDX-License-Identifier: AGPL-3.0-or-later
/** How a transaction got its category, in words. */
import type { Transaction } from "./api";

export type CategorySource = NonNullable<Transaction["category_source"]>;

type Provenance = Pick<Transaction, "category_id" | "category_source" | "suggested_category_id" | "splits">;

const LABELS: ReadonlyMap<CategorySource, string> = new Map<CategorySource, string>([
  ["person", "You chose this"],
  ["file", "From the file"],
  ["payee", "Payee default"],
  ["learned", "Learned from you"],
  ["dictionary", "Merchant list"],
  ["keyword", "Keyword"],
]);

const GUESSES: ReadonlySet<string> = new Set<CategorySource>(["learned", "dictionary", "keyword"]);

/**
 * Who or what decided the row's category. Null when there is nothing to say:
 * nobody decided, or the category is from before sources were recorded.
 * `ruleName` is the name of the rule the row points at, when it still exists.
 */
export function sourceLabel(row: Pick<Transaction, "category_source">, ruleName?: string): string | null {
  const source = row.category_source ?? null;
  if (source === null) {
    return null;
  }
  if (source === "rule") {
    return ruleName === undefined ? "A rule that no longer exists" : `Rule: ${ruleName}`;
  }
  return LABELS.get(source) ?? null;
}

/** A source PerFin worked out by itself; only these carry a confidence worth showing. */
export function isGuess(source: Transaction["category_source"]): boolean {
  return source != null && GUESSES.has(source);
}

/** `"0.90"` as `"90%"`. Null for anything that is not a share between 0 and 1. */
export function confidenceText(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  if (!/^[0-9.]+$/.test(text)) {
    return null;
  }
  const share = Number(text);
  return Number.isFinite(share) && share <= 1 ? `${String(Math.round(share * 100))}%` : null;
}

/** A row PerFin has only a suggestion for: no category of its own, and not split. */
export function isSuggestion(row: Provenance): boolean {
  return row.category_id === null && row.suggested_category_id != null && row.splits.length === 0;
}
