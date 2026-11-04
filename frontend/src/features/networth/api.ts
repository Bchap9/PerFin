// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type NetWorthHistory = Schemas["NetWorthHistory"];
export type NetWorthLatest = Schemas["NetWorthLatest"];
export type NetWorthProjection = Schemas["NetWorthProjection"];
export type NetWorthRange = "1m" | "3m" | "6m" | "ytd" | "1y" | "3y" | "5y" | "max";
export type Granularity = Schemas["Granularity"];

export const NET_WORTH_RANGES: readonly NetWorthRange[] = ["1m", "3m", "6m", "ytd", "1y", "3y", "5y", "max"];

export const netWorthKeys = {
  history: (range: string, granularity: string) => [...queryRoots.netWorth, "history", range, granularity],
  latest: [...queryRoots.netWorth, "latest"],
  projection: (months: number) => [...queryRoots.netWorth, "projection", months],
} as const;

export const getHistory = (range: NetWorthRange, granularity: Granularity | "") =>
  unwrap(api.GET("/api/v1/networth", { params: { query: { range, ...(granularity === "" ? {} : { granularity }) } } }));

export const getLatest = () => unwrap(api.GET("/api/v1/networth/latest"));

export const getProjection = (months: number) =>
  unwrap(api.GET("/api/v1/networth/projection", { params: { query: { months } } }));
