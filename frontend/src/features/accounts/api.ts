// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for accounts, balances and the shared ledger access rules. */
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { useAuthSession, type Role } from "../auth/session";

type Schemas = components["schemas"];

export type Account = Schemas["AccountOut"];
export type AccountType = Schemas["AccountType"];
export type AccountCreate = Schemas["AccountCreate"];
export type AccountUpdate = Schemas["AccountUpdate"];
export type BalanceHistory = Schemas["BalanceHistoryOut"];
export type BalanceSnapshot = Schemas["SnapshotOut"];

export const accountKeys = {
  all: [...queryRoots.accounts, "all"],
  one: (id: string) => [...queryRoots.accounts, "one", id],
  balances: (id: string, start: string | null, end: string | null) => [
    ...queryRoots.accounts,
    "balances",
    id,
    start,
    end,
  ],
} as const;

const PAGE_LIMIT = 200;
const MAX_PAGES = 25;

/** Every account the caller may see, newest page first (cursor walk, bounded). */
export async function listAllAccounts(): Promise<Account[]> {
  const result: Account[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const current: string | null = cursor;
    const data: Schemas["AccountPage"] = await unwrap(
      api.GET("/api/v1/accounts", {
        params: {
          query: {
            limit: PAGE_LIMIT,
            ...(current === null ? {} : { cursor: current }),
          },
        },
      }),
    );
    result.push(...data.items);
    cursor = data.next_cursor;
    if (cursor === null) {
      break;
    }
  }
  return result;
}

export function getAccount(id: string): Promise<Account> {
  return unwrap(
    api.GET("/api/v1/accounts/{account_id}", {
      params: { path: { account_id: id } },
    }),
  );
}

export function createAccount(body: AccountCreate): Promise<Account> {
  return unwrap(api.POST("/api/v1/accounts", { body }));
}

export function updateAccount(
  id: string,
  body: AccountUpdate,
): Promise<Account> {
  return unwrap(
    api.PATCH("/api/v1/accounts/{account_id}", {
      params: { path: { account_id: id } },
      body,
    }),
  );
}

export function closeAccount(
  id: string,
  closedOn: string | null,
): Promise<Account> {
  return unwrap(
    api.POST("/api/v1/accounts/{account_id}/close", {
      params: { path: { account_id: id } },
      body: closedOn === null ? null : { closed_on: closedOn },
    }),
  );
}

export function reopenAccount(id: string): Promise<Account> {
  return unwrap(
    api.POST("/api/v1/accounts/{account_id}/reopen", {
      params: { path: { account_id: id } },
    }),
  );
}

export function getBalanceHistory(
  id: string,
  start: string | null,
  end: string | null,
): Promise<BalanceHistory> {
  return unwrap(
    api.GET("/api/v1/accounts/{account_id}/balances", {
      params: {
        path: { account_id: id },
        query: {
          ...(start === null ? {} : { start }),
          ...(end === null ? {} : { end }),
        },
      },
    }),
  );
}

export function addManualBalance(
  id: string,
  asOfDate: string,
  amount: string,
  currency: string,
): Promise<BalanceSnapshot> {
  return unwrap(
    api.POST("/api/v1/accounts/{account_id}/balances", {
      params: { path: { account_id: id } },
      body: { as_of_date: asOfDate, balance: { amount, currency } },
    }),
  );
}

export function useAccounts() {
  return useQuery({ queryKey: accountKeys.all, queryFn: listAllAccounts });
}

export type AccountTypeSubtypes = components["schemas"]["AccountTypeSubtypes"];

function listSubtypes(): Promise<AccountTypeSubtypes[]> {
  return unwrap(api.GET("/api/v1/accounts/subtypes"));
}

/** The kinds of account within each type (Roth IRA, 529, HELOC...); static, fetched once. */
export function useAccountSubtypes() {
  return useQuery({
    queryKey: [...queryRoots.accounts, "subtypes"],
    queryFn: listSubtypes,
    staleTime: Infinity,
  });
}

/** The subtype choices of one type, as select options (empty when the type has none). */
export function subtypeOptions(
  catalogue: readonly AccountTypeSubtypes[] | undefined,
  type: AccountType,
): { value: string; label: string }[] {
  const entry = catalogue?.find((item) => item.type === type);
  return (entry?.subtypes ?? []).map((subtype) => ({
    value: subtype.code,
    label: subtype.label,
  }));
}

/** Roles that may change ledger data (viewers read only). The server enforces it again. */
export function canWriteLedger(role: Role | null | undefined): boolean {
  return role === "owner" || role === "admin" || role === "member";
}

/** Whether the signed-in user may write at all in the active household. */
export function useCanWrite(): boolean {
  const { session } = useAuthSession();
  return canWriteLedger(session?.household?.role);
}

/** Account types in display order with their group labels. */
export const ACCOUNT_TYPES: readonly { type: AccountType; label: string }[] = [
  { type: "checking", label: "Checking" },
  { type: "savings", label: "Savings" },
  { type: "cash", label: "Cash" },
  { type: "credit_card", label: "Credit cards" },
  { type: "brokerage", label: "Brokerage" },
  { type: "retirement", label: "Retirement" },
  { type: "loan", label: "Loans" },
  { type: "mortgage", label: "Mortgages" },
  { type: "line_of_credit", label: "Lines of credit" },
  { type: "manual_asset", label: "Manual assets" },
  { type: "manual_liability", label: "Manual liabilities" },
];

export interface AccountGroup {
  type: AccountType;
  label: string;
  accounts: Account[];
}

/** Accounts grouped by type in a fixed order, groups with no account left out, accounts by name. */
export function groupAccounts(accounts: readonly Account[]): AccountGroup[] {
  return ACCOUNT_TYPES.map(({ type, label }) => ({
    type,
    label,
    accounts: accounts
      .filter((account) => account.type === type)
      .sort((a, b) => a.name.localeCompare(b.name)),
  })).filter((group) => group.accounts.length > 0);
}

export function accountLabel(
  account: Pick<Account, "name" | "nickname">,
): string {
  return account.nickname !== null && account.nickname !== ""
    ? account.nickname
    : account.name;
}
