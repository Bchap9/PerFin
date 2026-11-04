// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for the review queue and the pure builders of its resolutions. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type ReviewItem = Schemas["ReviewItemOut"];
export type ReviewKind = Schemas["ReviewKind"];
export type ReviewItemState = Schemas["ReviewItemState"];
export type ResolveBody = Schemas["ResolveDuplicate"] | Schemas["ResolveUnknownAccount"] | Schemas["ResolveUnparsed"];

export const reviewKeys = {
  list: (state: ReviewItemState | "all") => [...queryRoots.review, "list", state],
} as const;

export type ReviewFilter = ReviewItemState | "all";

export async function listReviewItems(state: ReviewFilter): Promise<ReviewItem[]> {
  const out: ReviewItem[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < 20; page += 1) {
    const current: string | null = cursor;
    const data: Schemas["ReviewPage"] = await unwrap(
      api.GET("/api/v1/review", { params: { query: { state, limit: 200, ...(current === null ? {} : { cursor: current }) } } }),
    );
    out.push(...data.items);
    cursor = data.next_cursor;
    if (cursor === null) {
      break;
    }
  }
  return out;
}

export function resolveItem(id: string, body: ResolveBody): Promise<ReviewItem> {
  return unwrap(api.POST("/api/v1/review/{item_id}/resolve", { params: { path: { item_id: id } }, body }));
}

export function dismissItem(id: string): Promise<ReviewItem> {
  return unwrap(api.POST("/api/v1/review/{item_id}/dismiss", { params: { path: { item_id: id } } }));
}

// ---- payload readers ----------------------------------------------------------

export function payloadString(item: ReviewItem, key: string): string | null {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return typeof value === "string" ? value : null;
}

export function payloadIds(item: ReviewItem, key: string): string[] {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

// ---- resolution builders ---------------------------------------------------------

/** `duplicate_of` is required when an imported row has several candidates, and ignored for a repeated file. */
export function duplicateResolution(
  item: ReviewItem,
  decision: "confirm" | "reject",
  chosenCandidate: string | null,
): Schemas["ResolveDuplicate"] {
  const candidates = payloadIds(item, "candidates");
  const duplicateOf = decision === "confirm" ? (chosenCandidate ?? (candidates.length === 1 ? (candidates[0] ?? null) : null)) : null;
  return { kind: "possible_duplicate", decision, ...(duplicateOf === null ? {} : { duplicate_of: duplicateOf }) };
}

export function accountResolution(accountId: string, remember: boolean): Schemas["ResolveUnknownAccount"] {
  return { kind: "unknown_account", account_id: accountId, remember };
}

export function reprocessResolution(): Schemas["ResolveUnparsed"] {
  return { kind: "unparsed", action: "reprocess" };
}
