// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Exchange rates (`/fx-rates`, docs/API.md "FX rates notes"). A rate says
 * `1 base = rate quote` on a day; there is one per household, pair and day.
 * Everyone in the household reads them; an owner or admin sets and deletes.
 */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type FxRate = Schemas["FxRateOut"];
export type FxRateSet = Schemas["FxRateSet"];
export type FxRatePage = Schemas["FxRatePage"];

/** The pair to list, in the direction the rates were stored; an empty side matches any currency. */
export interface FxPairFilter {
  base: string;
  quote: string;
}

export const NO_PAIR: FxPairFilter = { base: "", quote: "" };

/** `source` of a rate a person entered; anything else names the provider that fetched it. */
export const MANUAL_SOURCE = "manual";

export const FX_PAGE_SIZE = 100;

export const fxKeys = {
  all: [...queryRoots.settings, "fx-rates"],
  list: (pair: FxPairFilter) => [...queryRoots.settings, "fx-rates", "list", pair],
} as const;

/** Totals that are converted with these rates, to refresh after a change. */
export const FX_DEPENDENTS = [
  fxKeys.all,
  queryRoots.netWorth,
  queryRoots.reports,
  queryRoots.dashboard,
  queryRoots.investments,
  queryRoots.loans,
] as const;

/** Newest day first, then pair. */
export function listFxRates(pair: FxPairFilter, cursor: string | null): Promise<FxRatePage> {
  return unwrap(
    api.GET("/api/v1/fx-rates", {
      params: {
        query: {
          limit: FX_PAGE_SIZE,
          ...(pair.base === "" ? {} : { base: pair.base }),
          ...(pair.quote === "" ? {} : { quote: pair.quote }),
          ...(cursor === null ? {} : { cursor }),
        },
      },
    }),
  );
}

/** Set the manual rate of a pair and day; it replaces whatever that pair had that day. */
export function setFxRate(body: FxRateSet, idempotencyKey: string): Promise<FxRate> {
  return unwrap(api.POST("/api/v1/fx-rates", { body, params: { header: { "Idempotency-Key": idempotencyKey } } }));
}

export async function deleteFxRate(rateId: string): Promise<void> {
  await unwrap(api.DELETE("/api/v1/fx-rates/{rate_id}", { params: { path: { rate_id: rateId } } }));
}
