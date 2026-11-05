// SPDX-License-Identifier: AGPL-3.0-or-later
/** Fixtures and API mocks shared by the planning feature tests (imported by tests only). */
import { authedSession, json, type Handler, type SessionOptions } from "../auth/testing";

export const CAT_FOOD = "c0000000-0000-4000-8000-000000000001";
export const CAT_DINING = "c0000000-0000-4000-8000-000000000002";
export const CAT_RENT = "c0000000-0000-4000-8000-000000000003";
export const PAYEE_CAFE = "d0000000-0000-4000-8000-000000000001";
export const TAG_FUN = "e0000000-0000-4000-8000-000000000001";
export const ACCOUNT_CHECKING = "a0000000-0000-4000-8000-000000000001";

const STAMP = "2026-11-01T10:00:00Z";

export const category = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  kind: "expense",
  parent_id: null,
  system_key: null,
  tax_treatment: null,
  created_at: STAMP,
  updated_at: STAMP,
  ...extra,
});

export const CATEGORIES = [
  category(CAT_FOOD, "Food"),
  category(CAT_DINING, "Dining", { parent_id: CAT_FOOD }),
  category(CAT_RENT, "Rent"),
];

export const money = (amount: string, currency = "USD") => ({ amount, currency });

export function referenceHandlers(): Record<string, Handler> {
  return {
    "GET /api/v1/categories": () => json({ items: CATEGORIES, next_cursor: null }),
    "GET /api/v1/payees": () =>
      json({
        items: [{ id: PAYEE_CAFE, name: "Cafe Uno", normalized_name: "cafe uno", aliases: [], default_category_id: null, created_at: STAMP, updated_at: STAMP }],
        next_cursor: null,
      }),
    "GET /api/v1/tags": () => json({ items: [{ id: TAG_FUN, name: "fun", created_at: STAMP, updated_at: STAMP }], next_cursor: null }),
    "GET /api/v1/accounts": () =>
      json({
        items: [
          {
            id: ACCOUNT_CHECKING,
            name: "Checking",
            nickname: null,
            type: "checking",
            currency: "USD",
            balance: money("1200.50"),
            balance_date: "2026-11-03",
            is_asset: true,
            is_closed: false,
            include_in_net_worth: true,
            manual_balance: false,
            can_edit: true,
            asset_kind: null,
            closed_on: null,
            institution_id: null,
            number_last4: null,
            opened_on: null,
            created_at: STAMP,
            updated_at: STAMP,
          },
        ],
        next_cursor: null,
      }),
    "GET /api/v1/households/:id": () => json({ id: "h-1", name: "Home", base_currency: "USD", created_at: STAMP, role: "owner" }),
  };
}

export function ownerSession(options: SessionOptions = {}): Record<string, Handler> {
  return { "GET /api/v1/auth/session": authedSession({ role: "owner", ...options }) };
}
