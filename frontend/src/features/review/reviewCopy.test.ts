// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReviewItem } from "./api";
import { explain } from "./reviewCopy";

function item(
  kind: ReviewItem["kind"],
  payload: Record<string, unknown>,
): ReviewItem {
  return {
    id: "r",
    kind,
    state: "open",
    document_id: "d",
    payload,
    resolution: null,
    resolved_at: null,
    resolved_by: null,
    created_at: "2026-11-05T00:00:00Z",
    next_step: null,
    can_resolve: true,
  };
}

/** Every reason the server raises has its own words and at least one step. */
const REASONS: readonly [ReviewItem["kind"], string][] = [
  ["unknown_account", "account_not_found"],
  ["unknown_account", "account_ambiguous"],
  ["unknown_account", "account_required"],
  ["unknown_account", "currency_mismatch"],
  ["unknown_account", "account_type_mismatch"],
  ["unparsed", "mapping_required"],
  ["unparsed", "mapping_ambiguous"],
  ["unparsed", "mapping_failed"],
  ["unparsed", "activity_unreadable"],
  ["unparsed", "extraction_failed"],
  ["unparsed", "format_not_supported"],
  ["unparsed", "encrypted"],
  ["unparsed", "no_records"],
  ["unparsed", "scan_failed"],
  ["unparsed", "container_incomplete"],
  ["low_confidence_field", "lines_skipped"],
  ["low_confidence_field", "low_confidence"],
  ["low_confidence_field", "reconciliation_failed"],
  ["low_confidence_field", "reconciliation_unavailable"],
  ["low_confidence_field", "investment_opening_basis_unknown"],
  ["low_confidence_field", "investment_lot_conflict"],
  ["low_confidence_field", "investment_out_of_order"],
  ["low_confidence_field", "investment_possible_duplicate"],
  ["low_confidence_field", "investment_low_confidence"],
  ["low_confidence_field", "position_mismatch"],
];

describe("review explanations", () => {
  it.each(REASONS)("%s / %s has its own explanation", (kind, reason) => {
    const fallback = explain(item(kind, {}));
    const found = explain(item(kind, { reason }));
    expect(found.what).not.toBe("");
    expect(found.steps.length).toBeGreaterThan(0);
    expect(found.what === fallback.what && found.title === fallback.title).toBe(
      false,
    );
  });

  it("lists the unknown actions of skipped rows", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "lines_skipped",
        count: 3,
        unknown_activity: 2,
        unknown_actions: [{ action: "mystery item", rows: 2 }],
      }),
    );
    expect(found.what).toBe(
      "3 rows of this file were not imported, 2 of them with an action PerFin does not know. Unknown actions: mystery item.",
    );
  });

  it("falls back to the kind's own words for an unknown reason", () => {
    expect(
      explain(item("rejected_email", { reason: "daily_digest" })).what,
    ).toBe("This email was not accepted.");
  });
});
