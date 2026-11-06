// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Security = Schemas["SecurityOut"];
export type SecurityCreate = Schemas["SecurityCreate"];
export type AssetClass = Schemas["AssetClass"];
export type Price = Schemas["PriceOut"];
export type Holdings = Schemas["HoldingsOut"];
export type Holding = Schemas["HoldingOut"];
export type Lot = Schemas["LotOut"];
export type Allocation = Schemas["AllocationOut"];
export type Performance = Schemas["PerformanceOut"];
export type PerformanceRange = Performance["range"];
export type InvestmentTxn = Schemas["InvestmentTxnOut"];
export type InvestmentTxnCreate = Schemas["InvestmentTxnCreate"];
export type InvestmentTxnType = Schemas["InvestmentTxnType"];
export type CostMethod = Schemas["CostMethod"];

export const PERFORMANCE_RANGES: readonly PerformanceRange[] = ["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max"];

export const investmentKeys = {
  securities: [...queryRoots.investments, "securities"],
  prices: (securityId: string) => [...queryRoots.investments, "prices", securityId],
  holdings: (account: string) => [...queryRoots.investments, "holdings", account],
  performance: (range: string, account: string) => [...queryRoots.investments, "performance", range, account],
  transactions: (account: string, security: string, type: string) => [
    ...queryRoots.investments,
    "transactions",
    account,
    security,
    type,
  ],
} as const;

const withCursor = (cursor: string | null) => (cursor === null ? {} : { cursor });

export function listSecurities(): Promise<Security[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/securities", { params: { query: { limit: 500, ...withCursor(cursor) } } })),
  );
}

export const createSecurity = (body: SecurityCreate) => unwrap(api.POST("/api/v1/securities", { body }));

export function listPrices(securityId: string): Promise<Price[]> {
  return collectPages((cursor) =>
    unwrap(
      api.GET("/api/v1/securities/{security_id}/prices", {
        params: { path: { security_id: securityId }, query: { limit: 200, ...withCursor(cursor) } },
      }),
    ),
  );
}

export const setPrice = (securityId: string, body: Schemas["PriceSet"]) =>
  unwrap(api.POST("/api/v1/securities/{security_id}/prices", { params: { path: { security_id: securityId } }, body }));

export const deletePrice = async (securityId: string, priceId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/securities/{security_id}/prices/{price_id}", {
      params: { path: { security_id: securityId, price_id: priceId } },
    }),
  );
};

export const getHoldings = (account: string) =>
  unwrap(api.GET("/api/v1/investments/holdings", { params: { query: account === "" ? {} : { account } } }));

export const getPerformance = (range: PerformanceRange, account: string) =>
  unwrap(
    api.GET("/api/v1/investments/performance", {
      params: { query: { range, ...(account === "" ? {} : { account }) } },
    }),
  );

export interface TxnFilter {
  account: string;
  security: string;
  type: string;
}

export function listInvestmentTxns(filter: TxnFilter): Promise<InvestmentTxn[]> {
  return collectPages((cursor) =>
    unwrap(
      api.GET("/api/v1/investments/transactions", {
        params: {
          query: {
            limit: 200,
            ...(filter.account === "" ? {} : { account: filter.account }),
            ...(filter.security === "" ? {} : { security: filter.security }),
            ...(filter.type === "" ? {} : { type: filter.type as InvestmentTxnType }),
            ...withCursor(cursor),
          },
        },
      }),
    ),
  );
}

export const createInvestmentTxn = (body: InvestmentTxnCreate) => unwrap(api.POST("/api/v1/investments/transactions", { body }));

export const deleteInvestmentTxn = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/investments/transactions/{txn_id}", { params: { path: { txn_id: id } } }));
};

/** Change an investment transaction (e.g. the cost basis of an opening position: its `price`). */
export const updateInvestmentTxn = (id: string, body: Schemas["InvestmentTxnUpdate"]) =>
  unwrap(api.PATCH("/api/v1/investments/transactions/{txn_id}", { params: { path: { txn_id: id } }, body }));
