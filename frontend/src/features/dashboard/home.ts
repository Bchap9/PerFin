// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The pure parts of Home (design note, stage 3, section 3): the greeting,
 * the spending periods and their comparison, the five largest categories,
 * the change of net worth this month, what is coming up and the order of
 * what needs a person. No React and no fetching here.
 */
import { addDays, addMonths, compareIsoDates, endOfMonth, formatDate, startOfMonth } from "../../lib/dates";
import {
  amountSign,
  compareAmounts,
  formatMoney,
  isDecimalString,
  isZeroAmount,
  negateAmount,
  subtractAmounts,
  sumAmounts,
} from "../../lib/money";

interface MoneyValue {
  amount: string;
  currency: string;
}

/** A cost reads without its sign: money out is stored negative. */
function magnitude(amount: string): string {
  return isDecimalString(amount) && amountSign(amount) < 0 ? negateAmount(amount) : amount;
}

// ---- Header -------------------------------------------------------------------

export type Greeting = "Good morning" | "Good afternoon" | "Good evening";

/** By the browser's clock: 5:00 to 11:59 morning, 12:00 to 17:59 afternoon, else evening. */
export function greeting(now: Date): Greeting {
  const hour = now.getHours();
  if (hour >= 5 && hour < 12) {
    return "Good morning";
  }
  if (hour >= 12 && hour < 18) {
    return "Good afternoon";
  }
  return "Good evening";
}

// ---- Spending this month --------------------------------------------------------

export interface SpendingPeriods {
  from: string;
  to: string;
  previousFrom: string;
  previousTo: string;
}

/** This month up to today, and last month up to the same day (or its last day). */
export function spendingPeriods(today: string): SpendingPeriods {
  const from = startOfMonth(today);
  const previousFrom = startOfMonth(addDays(from, -1));
  const sameDay = addMonths(today, -1);
  const lastDay = endOfMonth(previousFrom);
  const previousTo = compareIsoDates(sameDay, lastDay) > 0 || compareIsoDates(sameDay, previousFrom) < 0 ? lastDay : sameDay;
  return { from, to: today, previousFrom, previousTo };
}

export interface Comparison {
  text: string;
  tone: "up" | "down" | "neutral";
}

/**
 * This month's spending against the same point of last month, in words.
 * Spending less is good news (`up`); spending more is said plainly and not
 * coloured as an error (red means only "over" or "error").
 */
export function comparison(current: string, previous: string, previousMonth: string): Comparison {
  const now = Math.abs(Number(magnitude(current)));
  const before = Math.abs(Number(magnitude(previous)));
  if (!Number.isFinite(now) || !Number.isFinite(before) || before === 0) {
    return { text: `Nothing spent by this point in ${previousMonth}`, tone: "neutral" };
  }
  if (now === before) {
    return { text: `The same as this point in ${previousMonth}`, tone: "neutral" };
  }
  const percent = Math.round((Math.abs(now - before) / before) * 100);
  if (percent === 0) {
    return { text: `About the same as this point in ${previousMonth}`, tone: "neutral" };
  }
  return now < before
    ? { text: `${String(percent)}% less than this point in ${previousMonth}`, tone: "up" }
    : { text: `${String(percent)}% more than this point in ${previousMonth}`, tone: "neutral" };
}

export interface CategoryShare {
  /** The category id, `null` for no category, `"other"` for the rest. */
  key: string | null;
  label: string;
  /** A positive decimal string. */
  amount: string;
}

export const OTHER_KEY = "other";

/** The `n` largest top-level categories, then everything else as "Other". */
export function topCategories(
  rows: readonly { key: string | null; name: string | null; amount: MoneyValue }[],
  n = 5,
): CategoryShare[] {
  const shares = rows
    .map((row) => ({
      key: row.key,
      label: row.key === null ? "Uncategorised" : (row.name ?? "Unknown category"),
      amount: magnitude(row.amount.amount),
    }))
    .filter((row) => isDecimalString(row.amount) && !isZeroAmount(row.amount))
    .sort((a, b) => compareAmounts(b.amount, a.amount));
  const top = shares.slice(0, n);
  const rest = shares.slice(n);
  if (rest.length === 0) {
    return top;
  }
  const label = top.some((share) => share.label === "Other") ? "Everything else" : "Other";
  return [...top, { key: OTHER_KEY, label, amount: sumAmounts(rest.map((share) => share.amount)) }];
}

// ---- Net worth ----------------------------------------------------------------

/**
 * Net worth now against the last point of the series dated before the
 * month began; `null` when the series does not reach back that far.
 */
export function netWorthChange(
  net: string,
  points: readonly { as_of: string; net: MoneyValue }[],
  today: string,
): string | null {
  const monthStart = startOfMonth(today);
  const before = points.filter((point) => compareIsoDates(point.as_of, monthStart) < 0).at(-1);
  if (before === undefined || !isDecimalString(net) || !isDecimalString(before.net.amount)) {
    return null;
  }
  return subtractAmounts(net, before.net.amount);
}

// ---- Coming up ----------------------------------------------------------------

type Kind = "subscription" | "bill" | "income" | "other";
type Status = "suggested" | "active" | "confirmed" | "paused" | "ended" | "dismissed";

export interface UpcomingSubscription {
  id: string;
  name: string;
  kind: Kind;
  status: Status;
  next_due: string | null;
  amount: MoneyValue;
  amount_min: MoneyValue;
  amount_max: MoneyValue;
  price_change: { previous: MoneyValue; current: MoneyValue; on: string; pending: boolean } | null;
}

export interface UpcomingSchedule {
  id: string;
  name: string;
  kind: Kind;
  status: Status;
  active: boolean;
  next_due: string;
  amount_min: MoneyValue;
  amount_max: MoneyValue;
}

export interface ComingUpItem {
  id: string;
  name: string;
  date: string;
  /** Positive; whole units when `approximate`. */
  amount: string;
  currency: string;
  /** The amount varies: it is shown as "~" and the middle of its range. */
  approximate: boolean;
  note: string | null;
}

/** Running series: not a guess, not stopped, not dismissed. */
const RUNNING: ReadonlySet<Status> = new Set<Status>(["active", "confirmed"]);

function money(amount: string, currency: string): string {
  return formatMoney(amount, currency) ?? `${amount} ${currency}`;
}

function whenText(on: string, today: string): string {
  const thisMonth = startOfMonth(today);
  if (startOfMonth(on) === thisMonth) {
    return "this month";
  }
  if (startOfMonth(on) === startOfMonth(addDays(thisMonth, -1))) {
    return "last month";
  }
  return `on ${formatDate(on) ?? on}`;
}

function priceNote(change: NonNullable<UpcomingSubscription["price_change"]>, today: string): string | null {
  const before = magnitude(change.previous.amount);
  const after = magnitude(change.current.amount);
  if (!isDecimalString(before) || !isDecimalString(after)) {
    return null;
  }
  const order = compareAmounts(after, before);
  if (order === 0) {
    return null;
  }
  const by = money(order > 0 ? subtractAmounts(after, before) : subtractAmounts(before, after), change.current.currency);
  const note = `went ${order > 0 ? "up" : "down"} ${by} ${whenText(change.on, today)}`;
  return change.pending ? `${note}, seen once so far` : note;
}

function range(min: MoneyValue, max: MoneyValue): { low: string; high: string } | null {
  const a = magnitude(min.amount);
  const b = magnitude(max.amount);
  if (!isDecimalString(a) || !isDecimalString(b) || compareAmounts(a, b) === 0) {
    return null;
  }
  return compareAmounts(a, b) < 0 ? { low: a, high: b } : { low: b, high: a };
}

function middle(low: string, high: string): string {
  return String(Math.round((Number(low) + Number(high)) / 2));
}

function inWindow(due: string | null, today: string, last: string): due is string {
  return due !== null && compareIsoDates(due, today) >= 0 && compareIsoDates(due, last) <= 0;
}

/**
 * The next charges within `days` of `today`, from the subscriptions and the
 * other recurring items, ordered by date, each series once (the
 * subscription's entry wins: it knows the price change), at most `max`.
 */
export function comingUp(
  subscriptions: readonly UpcomingSubscription[],
  recurring: readonly UpcomingSchedule[],
  today: string,
  days = 30,
  max = 5,
): ComingUpItem[] {
  const last = addDays(today, days);
  const known = new Set(subscriptions.map((entry) => entry.id));
  const items: ComingUpItem[] = [];
  for (const entry of subscriptions) {
    if (!RUNNING.has(entry.status) || entry.kind === "income" || !inWindow(entry.next_due, today, last)) {
      continue;
    }
    const spread = range(entry.amount_min, entry.amount_max);
    const change = entry.price_change === null ? null : priceNote(entry.price_change, today);
    const approximate = change === null && spread !== null;
    items.push({
      id: entry.id,
      name: entry.name,
      date: entry.next_due,
      amount: approximate ? String(Math.round(Number(magnitude(entry.amount.amount)))) : magnitude(entry.amount.amount),
      currency: entry.amount.currency,
      approximate,
      note: change ?? (spread === null ? null : `usually ${money(spread.low, entry.amount.currency)} to ${money(spread.high, entry.amount.currency)}`),
    });
  }
  for (const entry of recurring) {
    if (known.has(entry.id) || !entry.active || !RUNNING.has(entry.status) || entry.kind === "income") {
      continue;
    }
    if (!inWindow(entry.next_due, today, last)) {
      continue;
    }
    const currency = entry.amount_min.currency;
    const spread = range(entry.amount_min, entry.amount_max);
    items.push({
      id: entry.id,
      name: entry.name,
      date: entry.next_due,
      amount: spread === null ? magnitude(entry.amount_min.amount) : middle(spread.low, spread.high),
      currency,
      approximate: spread !== null,
      note: spread === null ? null : `usually ${money(spread.low, currency)} to ${money(spread.high, currency)}`,
    });
  }
  return items
    .sort((a, b) => compareIsoDates(a.date, b.date) || a.name.localeCompare(b.name))
    .slice(0, max);
}

// ---- Needs you ----------------------------------------------------------------

/*
 * The Inbox order (design note, section 4.2), groups 1 to 4. A local copy:
 * lane I owns `inbox/order.ts`, and the integration task replaces this copy
 * with an import of it.
 */
const ACTION_GROUP: ReadonlyMap<string, number> = new Map([
  // 1. The money is wrong until someone answers.
  ["reconcile", 1],
  ["check_rows", 1],
  ["confirm_duplicate", 1],
  ["answer_sign", 1],
  // 2. The document cannot import until someone answers.
  ["choose_account", 2],
  ["map_columns", 2],
  ["choose_owner", 2],
  ["teach", 2],
  ["reprocess", 2],
  // 3. Something to note (and any action this client does not know).
  ["attach_receipt", 3],
  ["acknowledge", 3],
]);
const NOTE_GROUP = 3;

const QUESTIONS: ReadonlyMap<string, string> = new Map([
  ["reconcile", "Check the balance against the statement"],
  ["check_rows", "Check the rows PerFin was unsure of"],
  ["confirm_duplicate", "Is this a duplicate?"],
  ["answer_sign", "Which way does the money go?"],
  ["choose_account", "Which account is this?"],
  ["map_columns", "Which column is which?"],
  ["choose_owner", "Whose document is this?"],
  ["teach", "Show PerFin how to read this file"],
  ["reprocess", "PerFin could not read this file"],
  ["attach_receipt", "Is this the receipt for a transaction?"],
  ["acknowledge", "Something to note"],
]);

const LOOSE_QUESTIONS: ReadonlyMap<string, string> = new Map([
  ["possible_duplicate", "Is this a duplicate?"],
  ["unknown_account", "Which account is this?"],
  ["unknown_owner", "Whose is this?"],
  ["receipt_match", "Is this the receipt for a transaction?"],
  ["low_confidence_field", "Check a value PerFin was unsure of"],
  ["unparsed", "PerFin could not read this"],
]);

export interface NeedsYouRow {
  id: string;
  title: string;
  question: string;
  /** The document the row opens on the Inbox; `null` for an item of no document. */
  document: string | null;
}

/** Document cards in the Inbox order (oldest first inside a group), then the loose items. */
export function needsYouRows(
  cards: readonly { action: string; document: { id: string; original_filename: string | null; created_at: string } }[],
  loose: readonly { id: string; kind: string }[],
): NeedsYouRow[] {
  const ordered = cards
    .map((card, index) => ({ card, index, group: ACTION_GROUP.get(card.action) ?? NOTE_GROUP }))
    .sort(
      (a, b) =>
        a.group - b.group || a.card.document.created_at.localeCompare(b.card.document.created_at) || a.index - b.index,
    );
  return [
    ...ordered.map(({ card }) => ({
      id: card.document.id,
      title: card.document.original_filename ?? "A document with no name",
      question: QUESTIONS.get(card.action) ?? "Something to note",
      document: card.document.id,
    })),
    ...loose.map((item) => ({
      id: item.id,
      title: "An item of no document",
      question: LOOSE_QUESTIONS.get(item.kind) ?? "Something to check",
      document: null,
    })),
  ];
}

/** A suggested series, as much of it as Needs you shows. */
export interface SeriesWords {
  id: string;
  name: string;
  kind: string;
  occurrences: number;
}

/** An open change of a series, as much of it as Needs you shows. */
export interface ChangeWords {
  id: string;
  schedule_id: string;
  name: string;
  kind: "new" | "price_changed" | "missed" | "ended" | "charged_after_end" | "renewal_due" | "trial_converted";
  occurred_on: string;
  amount_before: MoneyValue | null;
  amount_after: MoneyValue | null;
}

function changeDate(iso: string): string {
  return formatDate(iso) ?? iso;
}

function changeCost(value: MoneyValue | null): string | null {
  return value === null ? null : formatMoney(magnitude(value.amount), value.currency);
}

/** One open change in words (the Inbox's subscription card says the same). */
function changeText(change: ChangeWords): string {
  const on = changeDate(change.occurred_on);
  switch (change.kind) {
    case "price_changed": {
      const before = changeCost(change.amount_before);
      const after = changeCost(change.amount_after);
      return `${before === null || after === null ? "Price changed" : `Price went from ${before} to ${after}`} on ${on}`;
    }
    case "missed":
      return `Expected on ${on}, not seen`;
    case "ended":
      return `Stopped on ${on}`;
    case "new":
      return `First seen on ${on}`;
    case "charged_after_end":
      return `Charged after you ended it, on ${on}`;
    case "renewal_due":
      return `Renews on ${on}`;
    case "trial_converted":
      return `Trial became paid on ${on}`;
  }
}

/**
 * The subscription cards of the Inbox (spec section 4.2, group 5) as rows:
 * one per series, the series with an open change first (newest change
 * first), then the suggested series with none (most charges first). A row
 * names the series and its newest change; a suggested series with no change
 * reads as a guess. A local copy of `inbox/order.ts::subscriptionCards`,
 * which Task 21 may import instead.
 */
export function needsYouSeriesRows(suggested: readonly SeriesWords[], changes: readonly ChangeWords[]): NeedsYouRow[] {
  const entries = new Map<string, { name: string; series: SeriesWords | null; changes: ChangeWords[] }>();
  for (const series of suggested) {
    entries.set(series.id, { name: series.name, series, changes: [] });
  }
  for (const change of changes) {
    const entry = entries.get(change.schedule_id);
    if (entry === undefined) {
      entries.set(change.schedule_id, { name: change.name, series: null, changes: [change] });
    } else {
      entry.changes.push(change);
    }
  }
  const all = [...entries].map(([id, entry]) => ({
    id,
    ...entry,
    changes: [...entry.changes].sort((a, b) => b.occurred_on.localeCompare(a.occurred_on)),
  }));
  const changed = all
    .filter((entry) => entry.changes.length > 0)
    .sort((a, b) => (b.changes[0]?.occurred_on ?? "").localeCompare(a.changes[0]?.occurred_on ?? ""));
  const quiet = all
    .filter((entry) => entry.changes.length === 0)
    .sort((a, b) => (b.series?.occurrences ?? 0) - (a.series?.occurrences ?? 0));
  return [...changed, ...quiet].map((entry) => {
    const [newest, ...rest] = entry.changes;
    const more = rest.length === 0 ? "" : `, and ${String(rest.length)} more ${rest.length === 1 ? "change" : "changes"}`;
    const guess = entry.series?.kind === "subscription" ? "Might be a subscription" : "Might be recurring";
    return {
      id: `series:${entry.id}`,
      title: entry.name,
      question: newest === undefined ? guess : `${changeText(newest)}${more}`,
      document: null,
    };
  });
}

// ---- Latest transactions ----------------------------------------------------------

const SOURCES: ReadonlyMap<string, string> = new Map([
  ["person", "You chose this"],
  ["file", "From the file"],
  ["rule", "By a rule"],
  ["payee", "Payee default"],
  ["learned", "Learned from you"],
  ["dictionary", "Merchant list"],
  ["keyword", "Keyword"],
]);

/** Who or what decided a row's category, in words; null when nothing was recorded. */
export function categorySourceText(source: string | null | undefined): string | null {
  return source === null || source === undefined ? null : (SOURCES.get(source) ?? null);
}
