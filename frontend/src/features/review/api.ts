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

export function payloadNumber(item: ReviewItem, key: string): number | null {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return typeof value === "number" ? value : null;
}

export function payloadIds(item: ReviewItem, key: string): string[] {
  const value: unknown = new Map(Object.entries(item.payload)).get(key);
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string")
    : [];
}

/**
 * One check a held brokerage statement failed (payload ``differences``):
 * what the statement prints, what its rows add up to, and the difference
 * (computed - printed). ``count`` is the number of positions for the
 * ``positions`` check.
 */
export interface Difference {
  check: string;
  printed: string;
  computed: string;
  difference: string;
  count: number | null;
}

export function payloadDifferences(item: ReviewItem): Difference[] {
  const value: unknown = new Map(Object.entries(item.payload)).get(
    "differences",
  );
  if (!Array.isArray(value)) {
    return [];
  }
  const out: Difference[] = [];
  for (const entry of value as unknown[]) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const check = field(entry, "check");
    const printed = field(entry, "printed");
    const computed = field(entry, "computed");
    const difference = field(entry, "difference");
    const count = field(entry, "count");
    if (
      typeof check !== "string" ||
      typeof printed !== "string" ||
      typeof computed !== "string" ||
      typeof difference !== "string"
    ) {
      continue;
    }
    out.push({
      check,
      printed,
      computed,
      difference,
      count: typeof count === "number" ? count : null,
    });
  }
  return out;
}

/**
 * One activity section of a brokerage statement whose rows are not the
 * total printed for them (payload ``sections``): the total's ``label``,
 * what it prints, what the ``rows`` read add up to and the difference
 * (computed - printed). ``source``: the section's own total line, or the
 * statement's cash-flow summary.
 */
export interface SectionDifference {
  label: string;
  printed: string;
  computed: string;
  difference: string;
  rows: number;
  source: string | null;
  page: number | null;
}

export function payloadSections(item: ReviewItem): SectionDifference[] {
  const value: unknown = new Map(Object.entries(item.payload)).get("sections");
  if (!Array.isArray(value)) {
    return [];
  }
  const out: SectionDifference[] = [];
  for (const entry of value as unknown[]) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const label = field(entry, "label");
    const printed = field(entry, "printed");
    const computed = field(entry, "computed");
    const difference = field(entry, "difference");
    const rows = field(entry, "rows");
    const source = field(entry, "source");
    const page = field(entry, "page");
    if (
      typeof label !== "string" ||
      typeof printed !== "string" ||
      typeof computed !== "string" ||
      typeof difference !== "string"
    ) {
      continue;
    }
    out.push({
      label,
      printed,
      computed,
      difference,
      rows: typeof rows === "number" ? rows : 0,
      source: typeof source === "string" ? source : null,
      page: typeof page === "number" ? page : null,
    });
  }
  return out;
}

/**
 * One activity line of a brokerage statement that is in doubt (payload
 * ``lines``): where it is printed, what it says and why it is doubted
 * (``fields``: ``reconciliation`` when no figure of the statement vouches
 * for it, ``trade_date`` for a date outside the period, ...).
 */
export interface HeldLine {
  index: number;
  page: number | null;
  date: string | null;
  type: string | null;
  description: string | null;
  amount: string | null;
  section: string | null;
  fields: string[];
}

export function payloadLines(item: ReviewItem): HeldLine[] {
  const value: unknown = new Map(Object.entries(item.payload)).get("lines");
  if (!Array.isArray(value)) {
    return [];
  }
  const text = (entry: object, key: string): string | null => {
    const found = field(entry, key);
    return typeof found === "string" ? found : null;
  };
  const out: HeldLine[] = [];
  for (const entry of value as unknown[]) {
    if (typeof entry !== "object" || entry === null) {
      continue;
    }
    const index = field(entry, "index");
    const page = field(entry, "page");
    const fields = field(entry, "fields");
    if (typeof index !== "number") {
      continue;
    }
    out.push({
      index,
      page: typeof page === "number" ? page : null,
      date: text(entry, "date"),
      type: text(entry, "type"),
      description: text(entry, "description"),
      amount: text(entry, "amount"),
      section: text(entry, "section"),
      fields: Array.isArray(fields)
        ? fields.filter((f): f is string => typeof f === "string")
        : [],
    });
  }
  return out;
}

/**
 * The doubts a person settles by saying "import as read" (the server's
 * ``CONFIRMABLE_DOUBTS``): whether the rows add up, and a kind of activity
 * the optional local model suggested for words PerFin did not know.
 */
const CONFIRMABLE_DOUBTS: ReadonlySet<string> = new Set([
  "reconciliation",
  "suggested_type",
]);

/** The item's rows carry a kind the local model suggested (``suggested_type``). */
export function hasSuggestedKinds(item: ReviewItem): boolean {
  return payloadIds(item, "fields").includes("suggested_type");
}

/**
 * A brokerage statement held back by its figures that may be imported as
 * printed: one that did not add up, or one with nothing to check against
 * (or the held lines of an imported one)
 * whose rows carry no other doubt than those above (the server's
 * ``acceptable_as_read``).
 * The item must name its statement (``statement_key``): the answer is for
 * that statement only, and an item raised before statements carried a key
 * is out of date (reprocess the document).
 */
export function canImportAsRead(item: ReviewItem): boolean {
  if (
    item.kind !== "low_confidence_field" ||
    payloadString(item, "statement_key") === null
  ) {
    return false;
  }
  const reason = item.reason ?? payloadString(item, "reason");
  if (reason === "investment_reconciliation_failed") {
    return true;
  }
  const fields = payloadIds(item, "fields");
  return (
    (reason === "investment_low_confidence" ||
      reason === "investment_lines_held") &&
    fields.length > 0 &&
    fields.every((field) => CONFIRMABLE_DOUBTS.has(field))
  );
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
    ...(accountId === null || accountId === ""
      ? {}
      : { account_id: accountId }),
    remember,
  };
}

export function layoutRequest(
  institutionId: string,
  reprocessSimilar: boolean,
  replace = false,
): Schemas["RememberLayout"] {
  return {
    institution_id: institutionId,
    reprocess_similar: reprocessSimilar,
    replace,
  };
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
    import_as_read: false,
  };
}

/** "Import as read": the item's held brokerage statement, imported as printed. */
export function importAsReadResolution(): Schemas["ResolveLowConfidence"] {
  return {
    kind: "low_confidence_field",
    transactions: [],
    confirm: false,
    import_as_read: true,
  };
}

// ---- corrections and teaching -------------------------------------------------------

export type Correction = Schemas["Correction"];
export type TeachRequest = Schemas["TeachRequest"];
export type TeachOut = Schemas["TeachOut"];

/** `POST /review/{id}/correct`: document facts (period, balances...) and row values. */
export function correctItem(id: string, body: Correction): Promise<ReviewItem> {
  return unwrap(
    api.POST("/api/v1/review/{item_id}/correct", {
      params: { path: { item_id: id } },
      body,
    }),
  );
}

/** `POST /documents/{id}/teach`: what the person labelled on the page. */
export function teachDocument(
  documentId: string,
  body: TeachRequest,
): Promise<TeachOut> {
  return unwrap(
    api.POST("/api/v1/documents/{document_id}/teach", {
      params: { path: { document_id: documentId } },
      body,
    }),
  );
}

/** Opening and closing balance a reconciliation item proposes (its figures). */
export function balanceCorrection(
  opening: string,
  closing: string,
  resolve: boolean,
): Correction {
  const document: NonNullable<Correction["document"]> = {};
  if (opening.trim() !== "") {
    document.opening_balance = opening.trim();
  }
  if (closing.trim() !== "") {
    document.closing_balance = closing.trim();
  }
  return { document, resolve };
}
