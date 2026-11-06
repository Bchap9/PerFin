// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReviewItem } from "./api";
import { differenceText, explain } from "./reviewCopy";

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
  ["low_confidence_field", "investment_same_name_securities"],
  ["low_confidence_field", "investment_possible_duplicate"],
  ["low_confidence_field", "investment_low_confidence"],
  ["low_confidence_field", "investment_reconciliation_failed"],
  ["low_confidence_field", "position_mismatch"],
  ["possible_duplicate", "statement_overlap"],
  ["possible_duplicate", "same_content"],
  ["possible_duplicate", "near_match"],
  ["unparsed", "loan_unmatched"],
  ["low_confidence_field", "rows_skipped"],
  ["low_confidence_field", "balance_mismatch"],
  ["low_confidence_field", "date_order_assumed"],
  ["low_confidence_field", "truncated"],
  ["low_confidence_field", "currency_mismatch"],
  ["low_confidence_field", "payslip_does_not_add_up"],
  ["low_confidence_field", "tax_form_low_confidence"],
  ["low_confidence_field", "tax_form_unread"],
  ["low_confidence_field", "loan_low_confidence"],
  ["low_confidence_field", "loan_payment_out_of_range"],
  ["low_confidence_field", "loan_reconciliation_failed"],
  ["low_confidence_field", "loan_currency_mismatch"],
  ["low_confidence_field", "loan_rate_differs"],
  ["low_confidence_field", "investment_date_out_of_range"],
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

  it("names both periods of an overlapping statement", () => {
    const found = explain(
      item("possible_duplicate", {
        reason: "statement_overlap",
        period_start: "2026-01-01",
        period_end: "2026-01-31",
        other_period_start: "2026-01-01",
        other_period_end: "2026-01-31",
      }),
    );
    expect(found.title).toBe("Statement already imported?");
    expect(found.what).toContain("Jan 1, 2026 – Jan 31, 2026");
  });

  it("says what did not add up on a held brokerage statement, with the difference", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "investment_reconciliation_failed",
        statement_key: "k".repeat(64),
        currency: "USD",
        differences: [
          {
            check: "total_value",
            printed: "52360.250000",
            computed: "53060.250000",
            difference: "700.000000",
          },
        ],
      }),
    );
    expect(found.title).toBe("Statement does not add up");
    expect(found.what).toContain(
      "Account value: the holdings plus the cash come to $53,060.25, but the statement prints an account value of $52,360.25 (a difference of $700.00).",
    );
    expect(found.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      true,
    );
  });

  it("offers the 'import as read' step only on an item naming its statement", () => {
    const steps = (payload: Record<string, unknown>) =>
      explain(item("low_confidence_field", payload)).steps;
    const asRead = (step: string) => step.startsWith("“Import as read”");
    expect(
      steps({ reason: "investment_reconciliation_failed" }).some(asRead),
    ).toBe(false);
    expect(
      steps({
        reason: "investment_low_confidence",
        fields: ["reconciliation"],
      }).some(asRead),
    ).toBe(false);
    expect(
      steps({
        reason: "investment_low_confidence",
        fields: ["reconciliation"],
        statement_key: "k".repeat(64),
      }).some(asRead),
    ).toBe(true);
  });

  it.each([
    [
      "positions",
      "Positions: for 2 holdings, quantity × price comes to $80.00, but the statement prints a market value of $90.00 (a difference of $10.00).",
    ],
    [
      "holdings_total",
      "Holdings total: the holdings read add up to $80.00, but the statement prints a total of $90.00 (a difference of $10.00).",
    ],
    [
      "cash",
      "Cash: the opening cash plus this period's activity comes to $80.00, but the statement prints a closing cash of $90.00 (a difference of $10.00).",
    ],
    [
      "total_value",
      "Account value: the holdings plus the cash come to $80.00, but the statement prints an account value of $90.00 (a difference of $10.00).",
    ],
  ])("explains the %s check in plain words", (check, words) => {
    expect(
      differenceText(
        {
          check,
          printed: "90.000000",
          computed: "80.000000",
          difference: "-10.000000",
          count: 2,
        },
        "USD",
      ),
    ).toBe(words);
  });

  it("tells a statement with nothing to check from one with guessed dates", () => {
    expect(
      explain(
        item("low_confidence_field", {
          reason: "investment_low_confidence",
          fields: ["reconciliation"],
        }),
      ).title,
    ).toBe("Nothing to check against");
    expect(
      explain(
        item("low_confidence_field", {
          reason: "investment_low_confidence",
          fields: ["trade_date"],
        }),
      ).title,
    ).toBe("Dates may be wrong");
  });

  it("falls back to the kind's own words for an unknown reason", () => {
    expect(
      explain(item("rejected_email", { reason: "daily_digest" })).what,
    ).toBe("This email was not accepted.");
  });
});
