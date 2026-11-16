// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for transactions and the category/payee/tag lookups. */
import { useQuery, type QueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { getCategorisationSummary } from "../categories/api";
import { listRules, previewMerchantRule, ruleKeys } from "../rules/api";

export { newIdempotencyKey } from "../../lib/idempotency";

type Schemas = components["schemas"];

export type Transaction = Schemas["TransactionOut"];
export type TransactionPage = Schemas["TransactionPage"];
export type TransactionCreate = Schemas["TransactionCreate"];
export type TransactionUpdate = Schemas["TransactionUpdate"];
export type AutoPairResult = Schemas["AutoPairResult"];
export type SplitIn = Schemas["SplitIn"];
export type BulkAction = Schemas["BulkAction"];
export type BulkResult = Schemas["BulkResult"];
export type Category = Schemas["CategoryOut"];
export type Payee = Schemas["PayeeOut"];
export type Tag = Schemas["TagOut"];
export type ReviewState = Schemas["ReviewState"];
export type TransactionStatus = Schemas["TransactionStatus"];

export const txKeys = {
  lists: [...queryRoots.transactions, "list"],
  list: (filters: TransactionFilters) => [...queryRoots.transactions, "list", filters],
  one: (id: string) => [...queryRoots.transactions, "one", id],
  candidates: (id: string) => [...queryRoots.transactions, "candidates", id],
  /** Under the transactions root, so every change to the ledger refreshes the counts. */
  summary: [...queryRoots.transactions, "categorisation-summary"],
  /** Also under the root: the counts of a merchant-rule preview move with the ledger. */
  merchantPreview: (id: string, categoryId: string) => [...queryRoots.transactions, "merchant-preview", id, categoryId],
} as const;

/**
 * The merchant of a row by the name the server gave it, else its key with
 * capitals; `null` when no merchant was recognised.
 */
export function merchantName(row: Pick<Transaction, "merchant" | "merchant_key">): string | null {
  const name = (row.merchant ?? "").trim();
  if (name !== "") {
    return name;
  }
  const key = (row.merchant_key ?? "").trim();
  return key === "" ? null : key.replace(/(^| )[a-z]/g, (letter) => letter.toUpperCase());
}

/** A merchant rule can be made from a row that names a merchant and is not split (the server checks the rest). */
export function canTeach(row: Pick<Transaction, "merchant_key" | "splits">): boolean {
  return (row.merchant_key ?? "") !== "" && row.splits.length === 0;
}

export const taxonomyKeys = {
  categories: [...queryRoots.taxonomy, "categories"],
  payees: [...queryRoots.taxonomy, "payees"],
  tags: [...queryRoots.taxonomy, "tags"],
} as const;

export interface TransactionFilters {
  account: string;
  dateFrom: string;
  dateTo: string;
  /** A category id, `"none"` for uncategorised, or empty. */
  category: string;
  payee: string;
  tag: string;
  reviewState: ReviewState | "";
  q: string;
  /** A document id: only the rows imported from that file. Empty for all. */
  document: string;
  /** A recurring series id: only the charges linked to it. Empty for all. */
  subscription: string;
  /** Only rows that carry a suggestion and no category. */
  suggested: boolean;
}

export const EMPTY_FILTERS: TransactionFilters = {
  account: "",
  dateFrom: "",
  dateTo: "",
  category: "",
  payee: "",
  tag: "",
  reviewState: "",
  q: "",
  document: "",
  subscription: "",
  suggested: false,
};

export const PAGE_SIZE = 50;

/** The query string object for the filters (empty filters are left out). */
export function filterQuery(filters: TransactionFilters) {
  return {
    ...(filters.account === "" ? {} : { account: [filters.account] }),
    ...(filters.dateFrom === "" ? {} : { date_from: filters.dateFrom }),
    ...(filters.dateTo === "" ? {} : { date_to: filters.dateTo }),
    ...(filters.category === "none" ? { uncategorized: true } : filters.category === "" ? {} : { category: [filters.category] }),
    ...(filters.payee === "" ? {} : { payee: [filters.payee] }),
    ...(filters.tag === "" ? {} : { tag: [filters.tag] }),
    ...(filters.reviewState === "" ? {} : { review_state: [filters.reviewState] }),
    ...(filters.q.trim() === "" ? {} : { q: filters.q.trim() }),
    ...(filters.document === "" ? {} : { document: filters.document }),
    ...(filters.subscription === "" ? {} : { subscription: filters.subscription }),
    ...(filters.suggested ? { suggested: true } : {}),
  };
}

export function listTransactions(filters: TransactionFilters, cursor: string | null): Promise<TransactionPage> {
  return unwrap(
    api.GET("/api/v1/transactions", {
      params: { query: { ...filterQuery(filters), limit: PAGE_SIZE, ...(cursor === null ? {} : { cursor }) } },
    }),
  );
}

export function getTransaction(id: string): Promise<Transaction> {
  return unwrap(api.GET("/api/v1/transactions/{transaction_id}", { params: { path: { transaction_id: id } } }));
}

/** Create a manual transaction. Retrying with the same `idempotencyKey` and body never creates it twice. */
export function createTransaction(body: TransactionCreate, idempotencyKey: string): Promise<Transaction> {
  return unwrap(api.POST("/api/v1/transactions", { params: { header: { "Idempotency-Key": idempotencyKey } }, body }));
}

export function updateTransaction(id: string, body: TransactionUpdate): Promise<Transaction> {
  return unwrap(api.PATCH("/api/v1/transactions/{transaction_id}", { params: { path: { transaction_id: id } }, body }));
}

export async function deleteTransaction(id: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/transactions/{transaction_id}", { params: { path: { transaction_id: id } } }));
}

/** How far apart (in days) the two legs of a transfer may be posted: the server's default. */
export const TRANSFER_WINDOW_DAYS = 3;

/** Run the transfer matcher over every account the caller may edit. */
export function autoPairTransfers(): Promise<AutoPairResult> {
  return unwrap(api.POST("/api/v1/transactions/transfers/auto-pair", { body: { window_days: TRANSFER_WINDOW_DAYS } }));
}

/**
 * Refetch what a change to the ledger can move: the lists, account balances,
 * budgets, goals, reports and the dashboard (the same families the
 * `transaction.*` events refresh, lib/sse.ts).
 */
export function invalidateLedger(queryClient: QueryClient): void {
  for (const queryKey of [
    queryRoots.transactions,
    queryRoots.accounts,
    queryRoots.budgets,
    queryRoots.goals,
    queryRoots.reports,
    queryRoots.dashboard,
  ]) {
    void queryClient.invalidateQueries({ queryKey });
  }
}

export function setSplits(id: string, splits: SplitIn[], categoryId?: string | null): Promise<Transaction> {
  return unwrap(
    api.POST("/api/v1/transactions/{transaction_id}/split", {
      params: { path: { transaction_id: id } },
      body: { splits, ...(categoryId === undefined ? {} : { category_id: categoryId }) },
    }),
  );
}

export function bulkAction(body: BulkAction): Promise<BulkResult> {
  return unwrap(api.POST("/api/v1/transactions/bulk", { body }));
}

export function transferCandidates(id: string): Promise<Transaction[]> {
  return unwrap(
    api.GET("/api/v1/transactions/{transaction_id}/transfer-candidates", {
      params: { path: { transaction_id: id }, query: {} },
    }),
  );
}

export function linkTransfer(id: string, counterpartId: string): Promise<Transaction> {
  return unwrap(
    api.POST("/api/v1/transactions/{transaction_id}/transfer", {
      params: { path: { transaction_id: id } },
      body: { counterpart_id: counterpartId },
    }),
  );
}

export async function unlinkTransfer(id: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/transactions/{transaction_id}/transfer", { params: { path: { transaction_id: id } } }));
}

const LOOKUP_LIMIT = 500;
const LOOKUP_PAGES = 20;

async function walk<T extends { id: string }>(
  fetchPage: (cursor: string | null) => Promise<{ items: T[]; next_cursor: string | null }>,
): Promise<T[]> {
  const out: T[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < LOOKUP_PAGES; page += 1) {
    const data = await fetchPage(cursor);
    out.push(...data.items);
    cursor = data.next_cursor;
    if (cursor === null) {
      break;
    }
  }
  return out;
}

export function listAllCategories(): Promise<Category[]> {
  return walk((cursor) =>
    unwrap(api.GET("/api/v1/categories", { params: { query: { limit: LOOKUP_LIMIT, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function listAllPayees(): Promise<Payee[]> {
  return walk((cursor) =>
    unwrap(api.GET("/api/v1/payees", { params: { query: { limit: LOOKUP_LIMIT, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function listAllTags(): Promise<Tag[]> {
  return walk((cursor) =>
    unwrap(api.GET("/api/v1/tags", { params: { query: { limit: LOOKUP_LIMIT, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function useCategories() {
  return useQuery({ queryKey: taxonomyKeys.categories, queryFn: listAllCategories });
}

export function usePayees() {
  return useQuery({ queryKey: taxonomyKeys.payees, queryFn: listAllPayees });
}

/** The counts beside the "Suggestions" and "No category" toggles. */
export function useCategorisationSummary() {
  return useQuery({ queryKey: txKeys.summary, queryFn: getCategorisationSummary });
}

/**
 * Rule names by id, for "Rule: <name>". Asked for only when a row on screen
 * was categorised by a rule; `undefined` until the answer is there.
 */
export function useRuleNames(enabled: boolean): ReadonlyMap<string, string> | undefined {
  const rules = useQuery({ queryKey: ruleKeys.list, queryFn: listRules, enabled });
  return useMemo(() => (rules.data === undefined ? undefined : new Map(rules.data.map((rule) => [rule.id, rule.name]))), [rules.data]);
}

/**
 * What a merchant rule from `transactionId` to `categoryId` would do. Asked
 * only when there is a category to ask about.
 */
export function useMerchantRulePreview(transactionId: string, categoryId: string, enabled = true) {
  return useQuery({
    queryKey: txKeys.merchantPreview(transactionId, categoryId),
    queryFn: () => previewMerchantRule({ transaction_id: transactionId, category_id: categoryId }),
    enabled: enabled && categoryId !== "",
  });
}

export function useTags() {
  return useQuery({ queryKey: taxonomyKeys.tags, queryFn: listAllTags });
}
