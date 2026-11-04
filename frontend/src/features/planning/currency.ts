// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { useAuthSession } from "../auth/session";

export const householdKey = (id: string) => [...queryRoots.settings, "household", id] as const;

/** The signed-in household, including its base currency. */
export function useHousehold() {
  const { session } = useAuthSession();
  const id = session?.household?.id ?? null;
  return useQuery({
    queryKey: householdKey(id ?? "none"),
    enabled: id !== null,
    queryFn: () => unwrap(api.GET("/api/v1/households/{household_id}", { params: { path: { household_id: id ?? "" } } })),
  });
}

/** The household base currency, or `null` until it is loaded. */
export function useHouseholdCurrency(): string | null {
  return useHousehold().data?.base_currency ?? null;
}
