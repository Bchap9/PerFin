// SPDX-License-Identifier: AGPL-3.0-or-later
/** Typed calls for the review queue and the pure builders of its resolutions. */
import { api, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { queryRoots } from "../../lib/queryKeys";

type Schemas = components["schemas"];

export type ReviewItem = Schemas["ReviewItemOut"];
export type ReviewKind = Schemas["ReviewKind"];
export type ReviewItemState = Schemas["ReviewItemState"];
export type ResolveBody =
  | Schemas["ResolveDuplicate"]
  | Schemas["ResolveUnknownAccount"]
  | Schemas["ResolveUnparsed"]
  | Schemas["ResolveReceiptMatch"]
  | Schemas["ResolveUnknownOwner"]
  | Schemas["ResolveLowConfidence"];

export const reviewKeys = {
  list: (state: ReviewItemState | "all") => [
    ...queryRoots.review,
    "list",
    state,
  ],
  one: (id: string) => [...queryRoots.review, "one", id],
} as const;

export type ReviewFilter = ReviewItemState | "all";

export async function listReviewItems(
  state: ReviewFilter,
): Promise<ReviewItem[]> {
  const out: ReviewItem[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < 20; page += 1) {
    const current: string | null = cursor;
    const data: Schemas["ReviewPage"] = await unwrap(
      api.GET("/api/v1/review", {
        params: {
          query: {
            state,
            limit: 200,
            ...(current === null ? {} : { cursor: current }),
          },
        },
      }),
    );
    out.push(...data.items);
    cursor = data.next_cursor;
    if (cursor === null) {
      break;
    }
  }
  return out;
}

export function resolveItem(
  id: string,
  body: ResolveBody,
): Promise<ReviewItem> {
  return unwrap(
    api.POST("/api/v1/review/{item_id}/resolve", {
      params: { path: { item_id: id } },
      body,
    }),
  );
}

/** One item with its page regions and alternative readings. */
export function getReviewItem(id: string): Promise<ReviewItem> {
  return unwrap(
    api.GET("/api/v1/review/{item_id}", { params: { path: { item_id: id } } }),
  );
}

export type RememberLayoutOut = Schemas["RememberLayoutOut"];
export type RuleDraft = Schemas["RuleDraftOut"];

export function rememberLayout(
  id: string,
  body: Schemas["RememberLayout"],
): Promise<RememberLayoutOut> {
  return unwrap(
    api.POST("/api/v1/review/{item_id}/remember-layout", {
      params: { path: { item_id: id } },
      body,
    }),
  );
}

export function getRuleDraft(id: string): Promise<RuleDraft> {
  return unwrap(
    api.GET("/api/v1/review/{item_id}/rule-draft", {
      params: { path: { item_id: id } },
    }),
  );
}

export function dismissItem(id: string): Promise<ReviewItem> {
  return unwrap(
    api.POST("/api/v1/review/{item_id}/dismiss", {
      params: { path: { item_id: id } },
    }),
  );
}

// ---- payload readers ----------------------------------------------------------

export function payloadString(item: ReviewItem, key: string): string | null {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return typeof value === "string" ? value : null;
}

export function payloadIds(item: ReviewItem, key: string): string[] {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string")
    : [];
}

/** A ranked transaction the receipt matcher suggests (``receipt_match`` items). */
export interface ReceiptSuggestion {
  transactionId: string;
  score: number;
  amount: string;
  days: number | null;
  alreadyLinked: boolean;
}

function field(entry: object, key: string): unknown {
  return new Map(Object.entries(entry)).get(key);
}

export function payloadSuggestions(item: ReviewItem): ReceiptSuggestion[] {
  const value: unknown = new Map(Object.entries(item.payload)).get(
    "suggestions",
  );
  if (!Array.isArray(value)) {
    return [];
  }
  const out: ReceiptSuggestion[] = [];
  for (const entry of value as unknown[]) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const id = field(entry, "transaction_id");
    const score = field(entry, "score");
    const amount = field(entry, "amount");
    const days = field(entry, "days");
    if (typeof id !== "string" || typeof score !== "number") {
      continue;
    }
    out.push({
      transactionId: id,
      score,
      amount: typeof amount === "string" ? amount : "",
      days: typeof days === "number" ? days : null,
      alreadyLinked: field(entry, "already_linked") === true,
    });
  }
  return out;
}

// ---- resolution builders ---------------------------------------------------------

/** `duplicate_of` is required when an imported row has several candidates, and ignored for a repeated file. */
export function duplicateResolution(
  item: ReviewItem,
  decision: "confirm" | "reject",
  chosenCandidate: string | null,
): Schemas["ResolveDuplicate"] {
  const candidates = payloadIds(item, "candidates");
  const duplicateOf =
    decision === "confirm"
      ? (chosenCandidate ??
        (candidates.length === 1 ? (candidates[0] ?? null) : null))
      : null;
  return {
    kind: "possible_duplicate",
    decision,
    ...(duplicateOf === null ? {} : { duplicate_of: duplicateOf }),
  };
}

export function accountResolution(
  accountId: string,
  remember: boolean,
): Schemas["ResolveUnknownAccount"] {
  return { kind: "unknown_account", account_id: accountId, remember };
}

export function reprocessResolution(): Schemas["ResolveUnparsed"] {
  return { kind: "unparsed", action: "reprocess" };
}

/** Attach the receipt to a transaction, or (``null``) record that none applies. */
export function receiptResolution(
  transactionId: string | null,
): Schemas["ResolveReceiptMatch"] {
  return { kind: "receipt_match", transaction_id: transactionId };
}

export function ownerResolution(
  userId: string,
  accountId: string | null,
  remember: boolean,
): Schemas["ResolveUnknownOwner"] {
  return {
    kind: "unknown_owner",
    user_id: userId,
    ...(accountId === null || accountId === "" ? {} : { account_id: accountId }),
    remember,
  };
}

export function layoutRequest(
  institutionId: string,
  reprocessSimilar: boolean,
): Schemas["RememberLayout"] {
  return { institution_id: institutionId, reprocess_similar: reprocessSimilar };
}

/** Institutions the server offers for a ``new_institution`` item. */
export function institutionChoices(
  item: ReviewItem,
): { id: string; label: string }[] {
  return (item.alternatives ?? [])
    .filter((alt) => alt.field === "institution_id")
    .map((alt) => ({ id: alt.value, label: alt.label ?? alt.value }));
}

// ---- corrections ------------------------------------------------------------------

export type Alternative = Schemas["Alternative"];
export type TransactionFix = Schemas["TransactionFix"];

/**
 * The fix an alternative reading stands for. Document readings of an amount
 * are unsigned, so the row's own sign is kept.
 */
export function fixFromAlternative(
  alt: Alternative,
  current: { amount: { amount: string; currency: string } },
): TransactionFix | null {
  if (alt.transaction_id === null || alt.transaction_id === undefined) {
    return null;
  }
  if (alt.field === "amount") {
    const negative = current.amount.amount.trim().startsWith("-");
    const magnitude = alt.value.replace(/^[-+]/, "");
    return {
      transaction_id: alt.transaction_id,
      amount: {
        amount: negative ? `-${magnitude}` : magnitude,
        currency: current.amount.currency,
      },
    };
  }
  if (alt.field === "posted_date") {
    return { transaction_id: alt.transaction_id, posted_date: alt.value };
  }
  return null;
}

/** Add a fix to the pending set; a later fix of the same field replaces an earlier one. */
export function mergeFix(
  fixes: readonly TransactionFix[],
  fix: TransactionFix,
): TransactionFix[] {
  const existing = fixes.find((f) => f.transaction_id === fix.transaction_id);
  const merged: TransactionFix = { ...existing, ...fix };
  return [
    ...fixes.filter((f) => f.transaction_id !== fix.transaction_id),
    merged,
  ];
}

export function lowConfidenceResolution(
  fixes: readonly TransactionFix[],
  confirm: boolean,
): Schemas["ResolveLowConfidence"] {
  return {
    kind: "low_confidence_field",
    transactions: [...fixes],
    confirm,
  };
}
