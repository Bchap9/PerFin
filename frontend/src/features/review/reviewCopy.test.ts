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
  ["unparsed", "positions_unreadable"],
  ["unparsed", "extraction_failed"],
  ["unparsed", "format_not_supported"],
  ["unparsed", "encrypted"],
  ["unparsed", "no_records"],
  ["unparsed", "no_rows"],
  ["unparsed", "scan_failed"],
  ["unparsed", "container_incomplete"],
  ["unparsed", "ocr_failed"],
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
  ["low_confidence_field", "investment_lines_held"],
  ["low_confidence_field", "investment_figures_differ"],
  ["low_confidence_field", "investment_short_position"],
  ["low_confidence_field", "accounts_not_read"],
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

  it("says that a header-only file has no rows, and sends nobody to the mapping editor", () => {
    const found = explain(
      item("unparsed", { reason: "no_rows", source: "table", table: 0 }),
    );
    expect(found.title).toBe("Nothing to import");
    expect(found.what).toBe("This file has no rows to import.");
    expect(found.steps.join(" ")).not.toContain("Map columns");
  });

  it("says of a document's pages that no dated rows were found on them", () => {
    const found = explain(
      item("unparsed", { reason: "no_rows", source: "pages" }),
    );
    expect(found.title).toBe("Nothing to import");
    expect(found.what).toBe(
      "PerFin found no dated rows to import on the pages of this document.",
    );
    expect(found.steps.join(" ")).toContain("Teach PerFin");
    expect(found.steps.join(" ")).not.toContain("Map columns");
  });

  it("sends an unreadable activity export to the mapping editor", () => {
    const found = explain(item("unparsed", { reason: "activity_unreadable" }));
    expect(found.steps.join(" ")).toContain("Map columns");
  });

  it.each([
    ["sign_unverified", "Money in or money out?", "Positive amounts are"],
    [
      "indicator_contradicts_sign",
      "The words and the signs disagree",
      "Positive amounts are",
    ],
    ["investment_columns", "Trades, not a cash ledger", "Investment activity"],
    ["no_header", "No header row", "Positive amounts are"],
  ])(
    "asks the question of a mapping suggestion's doubt: %s",
    (doubt, title, word) => {
      const general = explain(item("unparsed", { reason: "mapping_required" }));
      const found = explain(
        item("unparsed", {
          reason: "mapping_required",
          auto_import: "not_confident",
          suggestion: { confident: false, doubts: ["rows_unreadable", doubt] },
        }),
      );
      expect(found.title).toBe(title);
      expect(found.what).not.toBe(general.what);
      expect(found.why).toBeDefined();
      // The mapping editor is the answer to every doubt.
      expect(found.steps[0]).toContain("Map columns");
      expect(found.steps.join(" ")).toContain(word);
    },
  );

  it("asks for the sign when that is all that held the file", () => {
    // A mapping saved on trust that met a card account carries no doubt of its own.
    const found = explain(
      item("unparsed", {
        reason: "mapping_required",
        auto_import: "sign_unverified",
        suggestion: { confident: true, doubts: [] },
      }),
    );
    expect(found.title).toBe("Money in or money out?");
  });

  it("asks about the columns of a file without a header before its sign", () => {
    // Nothing named the columns, so "PerFin found the columns" is not yet true.
    const found = explain(
      item("unparsed", {
        reason: "mapping_required",
        auto_import: "not_confident",
        suggestion: { confident: false, doubts: ["no_header", "sign_unverified"] },
      }),
    );
    expect(found.title).toBe("No header row");
    expect(found.what).toContain("no header row");
  });

  it("keeps the general words for the other doubts, and names trades first", () => {
    const general = explain(item("unparsed", { reason: "mapping_required" }));
    expect(general.title).toBe("Needs a column mapping");
    expect(
      explain(
        item("unparsed", {
          reason: "mapping_required",
          auto_import: "not_confident",
          suggestion: { doubts: ["date_order_guessed"] },
        }),
      ),
    ).toEqual(general);
    expect(
      explain(
        item("unparsed", {
          reason: "mapping_required",
          suggestion: { doubts: ["sign_unverified", "investment_columns"] },
        }),
      ).title,
    ).toBe("Trades, not a cash ledger");
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

  it("says records of a statement file were not read", () => {
    const found = explain(
      item("low_confidence_field", { reason: "lines_skipped", count: 2, source: "extraction" }),
    );
    expect(found.title).toBe("Some records were not read");
    expect(found.what).toBe("2 records of this file could not be read and were not imported.");
    expect(found.steps.join(" ")).not.toContain("Teach PerFin");
    // Rows a table reader skipped keep their own words.
    expect(explain(item("low_confidence_field", { reason: "lines_skipped", count: 2 })).title).toBe(
      "Some rows were skipped",
    );
  });

  it("says one unread record in the singular", () => {
    const found = explain(
      item("low_confidence_field", { reason: "lines_skipped", count: 1, source: "extraction" }),
    );
    expect(found.what).toBe("1 record of this file could not be read and was not imported.");
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

  it("names the section that does not add up and the lines held for it", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "investment_lines_held",
        statement_key: "k".repeat(64),
        currency: "USD",
        fields: ["reconciliation"],
        held: 4,
        imported: 31,
        sections: [
          {
            label: "Total Checking Activity",
            printed: "-2443.210000",
            computed: "-2042.640000",
            difference: "400.570000",
            rows: 4,
            source: "total",
            page: 9,
          },
        ],
        unread_summary_lines: ["Margin Interest"],
        lines: [],
      }),
    );
    expect(found.title).toBe("A few lines need a look");
    expect(found.what).toContain(
      "The holdings and the other 31 activity lines were imported; 4 lines of this statement were held back",
    );
    expect(found.what).toContain(
      "Total Checking Activity: the statement prints -$2,443.21; the 4 lines read add up to -$2,042.64 (a difference of $400.57).",
    );
    expect(found.what).toContain(
      "cash-flow summary also lists Margin Interest, for which no lines were read.",
    );
    expect(found.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      true,
    );
  });

  it("does not offer to import a line with another doubt as read", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "investment_lines_held",
        statement_key: "k".repeat(64),
        fields: ["reconciliation", "trade_date"],
        held: 1,
        imported: 0,
      }),
    );
    expect(found.what).toContain(
      "The holdings were imported; 1 line of this statement was held back because the statement's own totals do not vouch for it.",
    );
    expect(found.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      false,
    );
  });

  it("says which lines carry a kind the local model suggested", () => {
    const held = explain(
      item("low_confidence_field", {
        reason: "investment_lines_held",
        statement_key: "k".repeat(64),
        fields: ["suggested_type"],
        held: 2,
        imported: 12,
      }),
    );
    expect(held.title).toBe("Check what the local model suggested");
    expect(held.what).toContain(
      "The holdings and the other 12 activity lines were imported; PerFin did not know the wording of 2 lines of this statement, so the local model suggested what kind of activity they are",
    );
    expect(held.why).toContain("never records a kind of activity on a model's word");
    expect(held.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      true,
    );
    // A line moving shares holds its statement: nothing imported yet.
    const whole = explain(
      item("low_confidence_field", {
        reason: "investment_low_confidence",
        statement_key: "k".repeat(64),
        fields: ["suggested_type"],
        lines: [{ index: 3, fields: ["suggested_type"], type: "buy" }],
      }),
    );
    expect(whole.title).toBe("Check what the local model suggested");
    expect(whole.what).toContain("1 line of this statement");
    expect(whole.what).toContain("nothing of the statement was imported yet");
    expect(whole.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      true,
    );
    // Beside another doubt the suggestion cannot be confirmed on a click.
    const dated = explain(
      item("low_confidence_field", {
        reason: "investment_low_confidence",
        statement_key: "k".repeat(64),
        fields: ["suggested_type", "trade_date"],
      }),
    );
    expect(dated.title).toBe("Dates may be wrong");
  });

  it("says a statement was imported although its totals disagree", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "investment_figures_differ",
        statement_key: "k".repeat(64),
        currency: "USD",
        differences: [
          {
            check: "total_value",
            printed: "50000.000000",
            computed: "50250.000000",
            difference: "250.000000",
          },
        ],
      }),
    );
    expect(found.title).toBe("Statement imported; its totals disagree");
    expect(found.what).toContain("so it was imported and nothing waits on you.");
    expect(found.what).toContain("a difference of $250.00");
    expect(found.steps.some((step) => step.startsWith("“Import as read”"))).toBe(
      false,
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

describe("what the import says about rows and accounts", () => {
  it("says how many rows of a bank statement wait", () => {
    const some = explain(
      item("low_confidence_field", {
        reason: "reconciliation_failed",
        rows_in_doubt: 1,
        rows_total: 24,
      }),
    );
    expect(some.why).toContain("the other 23 rows");
    expect(some.why).toContain("only 1 row is pending");
    const none = explain(
      item("low_confidence_field", {
        reason: "reconciliation_failed",
        rows_in_doubt: 0,
        rows_total: 24,
      }),
    );
    expect(none.why).toContain("vouches for every row");
    const all = explain(
      item("low_confidence_field", { reason: "reconciliation_failed" }),
    );
    expect(all.why).toContain("every row is pending");
  });

  it("names the accounts a document lists and does not print", () => {
    const one = explain(
      item("low_confidence_field", {
        reason: "accounts_not_read",
        accounts: ["0002"],
      }),
    );
    expect(one.what).toContain("the account ending in 0002");
    const two = explain(
      item("low_confidence_field", {
        reason: "accounts_not_read",
        accounts: ["0002", "0003"],
      }),
    );
    expect(two.what).toContain("the accounts ending in 0002, 0003");
  });

  it("tells a statement's missing cost basis from an export's", () => {
    const statement = explain(
      item("low_confidence_field", {
        reason: "investment_opening_basis_unknown",
        costed_at: "price",
      }),
    );
    expect(statement.what).toContain("at the statement's price");
    const exported = explain(
      item("low_confidence_field", {
        reason: "investment_opening_basis_unknown",
      }),
    );
    expect(exported.what).toContain("zero for now");
  });

  it("says a short position and its trades were left out", () => {
    const found = explain(
      item("low_confidence_field", {
        reason: "investment_short_position",
        positions: [{ symbol: "ZZSHT", quantity: "-100.00" }],
        lines_left_out: 2,
      }),
    );
    expect(found.what).toContain("a short position");
    expect(found.what).toContain("The 2 trades in that security");
  });

  it("explains a value the account list prints differently", () => {
    expect(
      differenceText(
        {
          check: "index_value",
          printed: "1000.00",
          computed: "1250.00",
          difference: "250.00",
          count: null,
        },
        "USD",
      ),
    ).toContain("list of accounts prints $1,000.00");
  });
});
