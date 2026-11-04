// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Reference data the planning screens share: categories, payees, tags and
 * accounts, each loaded completely (following cursors) and cached under its
 * query root so the SSE layer refreshes it (lib/sse.ts).
 */
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type Category = Schemas["CategoryOut"];
export type Payee = Schemas["PayeeOut"];
export type Tag = Schemas["TagOut"];
export type Account = Schemas["AccountOut"];

export const referenceKeys = {
  categories: [...queryRoots.taxonomy, "categories"],
  payees: [...queryRoots.taxonomy, "payees"],
  tags: [...queryRoots.taxonomy, "tags"],
  accounts: [...queryRoots.accounts, "all"],
} as const;

const MAX_PAGES = 40;

/** Follow `next_cursor` until the server has no more pages. */
export async function collectPages<T>(
  page: (cursor: string | null) => Promise<{ items: T[]; next_cursor: string | null }>,
): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  for (let index = 0; index < MAX_PAGES; index += 1) {
    const next = await page(cursor);
    items.push(...next.items);
    if (next.next_cursor === null) {
      break;
    }
    cursor = next.next_cursor;
  }
  return items;
}

export function listCategories(): Promise<Category[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/categories", { params: { query: { limit: 500, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function listPayees(): Promise<Payee[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/payees", { params: { query: { limit: 500, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function listTags(): Promise<Tag[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/tags", { params: { query: { limit: 500, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function listAccounts(): Promise<Account[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/accounts", { params: { query: { limit: 500, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export function useCategories() {
  return useQuery({ queryKey: referenceKeys.categories, queryFn: listCategories });
}

export function usePayees() {
  return useQuery({ queryKey: referenceKeys.payees, queryFn: listPayees });
}

export function useTags() {
  return useQuery({ queryKey: referenceKeys.tags, queryFn: listTags });
}

export function useAccounts() {
  return useQuery({ queryKey: referenceKeys.accounts, queryFn: listAccounts });
}

/** Lookup by id with a fallback label for something that was deleted. */
export function nameLookup(items: readonly { id: string; name: string }[] | undefined): (id: string | null) => string {
  const byId = new Map((items ?? []).map((item) => [item.id, item.name]));
  return (id) => (id === null ? "None" : (byId.get(id) ?? "Unknown"));
}
