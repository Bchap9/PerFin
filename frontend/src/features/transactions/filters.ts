// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The filters of the Transactions list, kept in the address so a list can be
 * linked to, bookmarked and reloaded, and the total of the rows shown.
 *
 * The keys are the ones of GET /transactions where there is one: `account`,
 * `category` (an id, or `none` for no category), `suggested=true`,
 * `merchant`, `subscription`, `document`, `date_from`, `date_to`, `q`, and
 * the three of the "More filters" box, `payee`, `tag` and `review_state`.
 * Defaults are left out; values that cannot be used (a date that is not a
 * date, an unknown review state) are ignored. Other search parameters (`tx`,
 * `always`) are not filters and are kept as they are.
 */
import { api, unwrap } from "../../lib/api/client";
import { isIsoDate } from "../../lib/dates";
import { amountSign, negateAmount, subtractAmounts, sumAmounts } from "../../lib/money";
import { EMPTY_FILTERS, filterQuery, PAGE_SIZE, type ReviewState, type Transaction, type TransactionFilters, type TransactionPage } from "./api";

/** The list's filters: those of `api.ts` and the merchant key. */
export interface ListFilters extends TransactionFilters {
  /** A merchant key: only the rows PerFin recognised as that merchant. */
  merchant: string;
}

export const NO_FILTERS: ListFilters = { ...EMPTY_FILTERS, merchant: "" };

/** Every search parameter that is a filter. */
export const FILTER_KEYS = [
  "account",
  "category",
  "suggested",
  "merchant",
  "subscription",
  "document",
  "date_from",
  "date_to",
  "q",
  "payee",
  "tag",
  "review_state",
] as const;

const REVIEW_STATES: readonly ReviewState[] = ["none", "pending", "confirmed"];

/** The router parses search values as JSON: `?q=2024` arrives as a number, `?suggested=true` as a boolean. */
function asText(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  return typeof value === "number" || typeof value === "boolean" ? String(value) : "";
}

export function fromSearch(search: Readonly<Record<string, unknown>>): ListFilters {
  const given = new Map(Object.entries(search));
  const text = (key: string): string => asText(given.get(key));
  const day = (key: string): string => (isIsoDate(text(key)) ? text(key) : "");
  const review = REVIEW_STATES.find((state) => state === text("review_state")) ?? "";
  return {
    account: text("account"),
    category: text("category"),
    suggested: text("suggested") === "true",
    merchant: text("merchant"),
    subscription: text("subscription"),
    document: text("document"),
    dateFrom: day("date_from"),
    dateTo: day("date_to"),
    q: text("q").slice(0, 100),
    payee: text("payee"),
    tag: text("tag"),
    reviewState: review,
  };
}

export function toSearch(filters: ListFilters): Record<string, string> {
  const pairs: [string, string][] = [
    ["account", filters.account],
    ["category", filters.category],
    ["suggested", filters.suggested ? "true" : ""],
    ["merchant", filters.merchant],
    ["subscription", filters.subscription],
    ["document", filters.document],
    ["date_from", filters.dateFrom],
    ["date_to", filters.dateTo],
    ["q", filters.q],
    ["payee", filters.payee],
    ["tag", filters.tag],
    ["review_state", filters.reviewState],
  ];
  return Object.fromEntries(pairs.filter(([, value]) => value !== ""));
}

/** True when anything narrows the list. */
export function hasFilters(filters: ListFilters): boolean {
  return Object.keys(toSearch(filters)).length > 0;
}

/**
 * The search object for the router: `previous` without its filters, then
 * `filters`. `suggested` is written as a boolean, so the address reads
 * `suggested=true` and not a quoted string.
 */
export function withFilters(previous: Readonly<Record<string, unknown>>, filters: ListFilters): Record<string, unknown> {
  const keys: ReadonlySet<string> = new Set(FILTER_KEYS);
  const kept = Object.entries(previous).filter(([key]) => !keys.has(key));
  const written = Object.entries(toSearch(filters)).map(([key, value]): [string, unknown] => [key, key === "suggested" ? true : value]);
  return Object.fromEntries([...kept, ...written]);
}

/** One page of the list for these filters. */
export function listFiltered(filters: ListFilters, cursor: string | null): Promise<TransactionPage> {
  return unwrap(
    api.GET("/api/v1/transactions", {
      params: {
        query: {
          ...filterQuery(filters),
          ...(filters.merchant === "" ? {} : { merchant: filters.merchant }),
          limit: PAGE_SIZE,
          ...(cursor === null ? {} : { cursor }),
        },
      },
    }),
  );
}

export interface Totals {
  /** Money in (positive amounts, refunds included), as a decimal string. */
  in: string;
  /** Money out, as a positive decimal string. */
  out: string;
  /** In less out. */
  net: string;
  /** The rows shown. */
  count: number;
  /** Rows left out of in and out because they move money between the person's own accounts. */
  transfers: number;
  /** The one currency of the rows added up, or null when there are none or more than one (nothing is added then). */
  currency: string | null;
}

/** In, out and net of the rows shown. A transfer counts in neither; amounts of different currencies are never added. */
export function totals(rows: readonly Pick<Transaction, "amount" | "transfer">[]): Totals {
  const counted = rows.filter((row) => row.transfer === null);
  const currencies = new Set(counted.map((row) => row.amount.currency));
  const none = { in: "0", out: "0", net: "0", count: rows.length, transfers: rows.length - counted.length };
  const [currency] = currencies;
  if (currencies.size !== 1 || currency === undefined) {
    return { ...none, currency: null };
  }
  const amounts = counted.map((row) => row.amount.amount);
  const moneyIn = sumAmounts(amounts.filter((amount) => amountSign(amount) > 0));
  const moneyOut = negateAmount(sumAmounts(amounts.filter((amount) => amountSign(amount) < 0)));
  return { ...none, in: moneyIn, out: moneyOut, net: subtractAmounts(moneyIn, moneyOut), currency };
}
