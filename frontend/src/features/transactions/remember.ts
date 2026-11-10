// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The words of a bank's description that are likely to come back next month.
 *
 * `Card Purchase 01/14 Corner Grocer #204 Springfield IL Card 7731` changes
 * its date and reference every time; a rule that should catch the next one
 * must match on what stays. `descriptionKey` drops the parts that hold a
 * digit (dates, references, card numbers) and the words a bank puts around
 * every payee, and keeps the rest in order. It is only a starting point: the
 * person sees and edits it before anything is saved.
 */

/** Words banks wrap around a payee; none of them says who was paid. */
const NOISE = new Set([
  ...["card", "purchase", "purchases", "with", "pin", "pos", "debit", "credit", "checkcard", "check", "visa"],
  ...["ach", "ppd", "web", "id", "des", "indn", "co", "ref", "pmt", "pmts", "payment", "online", "recurring"],
  ...["withdrawal", "deposit", "transaction", "point", "of", "sale", "from", "to", "the", "at", "on"],
]);

const MAX_WORDS = 4;

export function descriptionKey(raw: string | null | undefined): string {
  const words = (raw ?? "")
    .split(/[\s:,;/]+/)
    .map((word) => word.replace(/^[^\p{L}]+|[^\p{L}]+$/gu, ""))
    .filter((word) => word.length > 1 && !/\d/.test(word) && !NOISE.has(word.toLowerCase()));
  return words.slice(0, MAX_WORDS).join(" ");
}

/**
 * The rule the dialog saves: a transaction with no category whose description
 * contains `text` gets `categoryId`. The "no category" condition is what
 * keeps it from undoing a category someone chose by hand when the rule is
 * applied to transactions already in the ledger.
 */
export function categoryRule(text: string, categoryId: string, categoryName: string) {
  const value = text.trim();
  return {
    name: `${categoryName}: ${value}`.slice(0, 200),
    enabled: true,
    stop_processing: false,
    conditions: {
      type: "all" as const,
      conditions: [{ type: "payee" as const, op: "contains" as const, value }, { type: "category_unset" as const }],
    },
    actions: [{ type: "set_category" as const, category_id: categoryId }],
  };
}
