// SPDX-License-Identifier: AGPL-3.0-or-later
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";
import { collectPages } from "../planning/queries";

type Schemas = components["schemas"];

export type Asset = Schemas["AssetOut"];
export type AssetCreate = Schemas["AssetCreate"];
export type AssetUpdate = Schemas["AssetUpdate"];
export type AssetKind = Schemas["AssetKind"];
export type ValuationMethod = Schemas["ValuationMethod"];
export type Valuation = Schemas["ValuationOut"];

export const ASSET_KINDS: readonly AssetKind[] = ["property", "vehicle", "other"];
export const KIND_LABELS: Record<AssetKind, string> = { property: "Property", vehicle: "Vehicle", other: "Other" };
export const METHODS: readonly ValuationMethod[] = ["manual", "appraisal", "purchase_price", "index", "provider"];
export const METHOD_LABELS: Record<ValuationMethod, string> = {
  manual: "My estimate",
  appraisal: "Appraisal",
  purchase_price: "Purchase price",
  index: "Index",
  provider: "Provider",
};

export const assetKeys = {
  list: [...queryRoots.assets, "list"],
  one: (id: string) => [...queryRoots.assets, "one", id],
  valuations: (id: string) => [...queryRoots.assets, "valuations", id],
} as const;

export function listAssets(): Promise<Asset[]> {
  return collectPages((cursor) =>
    unwrap(api.GET("/api/v1/assets", { params: { query: { limit: 200, ...(cursor === null ? {} : { cursor }) } } })),
  );
}

export const getAsset = (id: string) => unwrap(api.GET("/api/v1/assets/{asset_id}", { params: { path: { asset_id: id } } }));
export const createAsset = (body: AssetCreate) => unwrap(api.POST("/api/v1/assets", { body }));
export const updateAsset = (id: string, body: AssetUpdate) =>
  unwrap(api.PATCH("/api/v1/assets/{asset_id}", { params: { path: { asset_id: id } }, body }));
export const deleteAsset = async (id: string): Promise<void> => {
  await unwrap(api.DELETE("/api/v1/assets/{asset_id}", { params: { path: { asset_id: id } } }));
};

export const listValuations = async (id: string): Promise<Valuation[]> =>
  (await unwrap(api.GET("/api/v1/assets/{asset_id}/valuations", { params: { path: { asset_id: id }, query: { limit: 500 } } }))).items;

export const addValuation = (id: string, body: Schemas["ValuationCreate"]) =>
  unwrap(api.POST("/api/v1/assets/{asset_id}/valuations", { params: { path: { asset_id: id } }, body }));

export const deleteValuation = async (id: string, valuationId: string): Promise<void> => {
  await unwrap(
    api.DELETE("/api/v1/assets/{asset_id}/valuations/{valuation_id}", { params: { path: { asset_id: id, valuation_id: valuationId } } }),
  );
};
