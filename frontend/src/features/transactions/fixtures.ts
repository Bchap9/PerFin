// SPDX-License-Identifier: AGPL-3.0-or-later
/** Synthetic fixtures for the ledger tests (imported by tests only). */
import type { Account } from "../accounts/api";
import { json, type Handler } from "../auth/testing";
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

export const CATEGORIES: Category[] = [
  {
    id: "c-1",
    name: "Groceries",
    kind: "expense",
    parent_id: null,
    system_key: null,
    tax_treatment: null,
    created_at: NOW,
    updated_at: NOW,
  },
  {
    id: "c-2",
    name: "Dining",
    kind: "expense",
    parent_id: null,
    system_key: null,
    tax_treatment: null,
    created_at: NOW,
    updated_at: NOW,
  },
];
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
    transfer: null,
    source_document_id: null,
    tag_ids: [],
    splits: [],
    created_at: NOW,
    updated_at: NOW,
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
