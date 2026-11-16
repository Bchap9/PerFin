// SPDX-License-Identifier: AGPL-3.0-or-later
/** Synthetic fixtures for the ledger tests (imported by tests only). */
import type { Account } from "../accounts/api";
import { json, type Handler } from "../auth/testing";
import type { CategorisationSummary } from "../categories/api";
import type { Category, Payee, Tag, Transaction } from "./api";

export const NOW = "2026-11-03T10:00:00Z";

export function account(
  id: string,
  name: string,
  extra: Partial<Account> = {},
): Account {
  return {
    id,
    name,
    nickname: null,
    type: "checking",
    currency: "USD",
    balance: { amount: "100.00", currency: "USD" },
    balance_date: "2026-11-03",
    can_edit: true,
    closed_on: null,
    created_at: NOW,
    updated_at: NOW,
    include_in_net_worth: true,
    institution_id: null,
    is_asset: true,
    is_closed: false,
    manual_balance: false,
    number_last4: null,
    opened_on: null,
    asset_kind: null,
    subtype: null,
    subtype_label: null,
    tax_treatment: null,
    ...extra,
  };
}

export function category(id: string, name: string, extra: Partial<Category> = {}): Category {
  return {
    id,
    name,
    kind: "expense",
    parent_id: null,
    system_key: null,
    tax_treatment: null,
    created_at: NOW,
    updated_at: NOW,
    ...extra,
  };
}

export const CATEGORIES: Category[] = [category("c-1", "Groceries"), category("c-2", "Dining")];
export const PAYEES: Payee[] = [
  {
    id: "p-1",
    name: "Corner Market",
    normalized_name: "corner market",
    aliases: [],
    default_category_id: null,
    created_at: NOW,
    updated_at: NOW,
  },
];
export const TAGS: Tag[] = [
  { id: "t-1", name: "trip", created_at: NOW, updated_at: NOW },
];

export function tx(id: string, extra: Partial<Transaction> = {}): Transaction {
  return {
    id,
    account_id: "a-1",
    posted_date: "2026-11-01",
    effective_date: null,
    amount: { amount: "-42.10", currency: "USD" },
    payee_id: "p-1",
    category_id: "c-1",
    raw_payee: "CORNER MARKET 12",
    memo: null,
    notes: null,
    status: "cleared",
    review_state: "none",
    confidence: null,
    merchant: null,
    merchant_key: null,
    category_source: null,
    category_confidence: null,
    category_rule_id: null,
    suggested_category_id: null,
    subscription_id: null,
    transfer: null,
    source_document_id: null,
    tag_ids: [],
    splits: [],
    created_at: NOW,
    updated_at: NOW,
    ...extra,
  };
}

/** A `GET /categorisation/summary` body. */
export function summary(extra: Partial<CategorisationSummary> = {}): CategorisationSummary {
  return {
    by_source: [],
    cleaner_version: 1,
    dictionary_merchants: 0,
    dictionary_version: 1,
    learned_merchants: 0,
    suggestions: 0,
    uncategorised: 0,
    up_to_date: true,
    ...extra,
  };
}

export const page =
  (items: unknown[], next: string | null = null): Handler =>
  () =>
    json({ items, next_cursor: next });

/** Handlers shared by the transaction screens. */
export function lookupHandlers(
  accounts: Account[] = [account("a-1", "Everyday")],
): Record<string, Handler> {
  return {
    "GET /api/v1/accounts": page(accounts),
    "GET /api/v1/categories": page(CATEGORIES),
    "GET /api/v1/payees": page(PAYEES),
    "GET /api/v1/tags": page(TAGS),
  };
}
