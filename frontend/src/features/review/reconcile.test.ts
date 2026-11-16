// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReconcileCause, StatementReconciliation } from "./api";
import {
  CAUSE_KINDS,
  causeText,
  expectedClosing,
  liveDifference,
  readPrinted,
  skippedText,
  statementToCheck,
  statusLine,
  teachHint,
} from "./reconcile";

function cause(
  kind: ReconcileCause["kind"],
  amount: string | null = "-12.00",
): ReconcileCause {
  return { kind, amount, page: null, bbox: null, transaction_ids: [] };
}

function statement(
  extra: Partial<StatementReconciliation> = {},
): StatementReconciliation {
  return {
    statement: 0,
    account_id: "a-1",
    account_name: "Everyday",
    can_edit: true,
    causes: [],
    opening: "500.00",
    closing: "434.50",
    previous_closing: null,
    currency: "USD",
    total: "-77.50",
    difference: "-12.00",
    lines_skipped: 0,
    period_start: "2026-10-01",
    period_end: "2026-10-31",
    printed_sign: "ledger",
    removed: [],
    rows: [],
    rows_truncated: false,
    status: "off",
    verification: "unverified",
    ...extra,
  };
}

describe("readPrinted", () => {
  it("reads a balance the way it is printed", () => {
    expect(readPrinted("1,204.18")).toBe("1204.18");
    expect(readPrinted("$1,204.18")).toBe("1204.18");
    expect(readPrinted("1,204.18 €")).toBe("1204.18");
    expect(readPrinted("$ 1,204.18")).toBe("1204.18");
    expect(readPrinted("(12.00)")).toBe("-12.00");
    expect(readPrinted("( 12.00 )")).toBe("-12.00");
    expect(readPrinted("-12")).toBe("-12");
    expect(readPrinted("-$12.00")).toBe("-12.00");
    expect(readPrinted("$-12.00")).toBe("-12.00");
    expect(readPrinted("- $ 12")).toBe("-12");
    expect(readPrinted(" 3 ")).toBe("3");
    expect(readPrinted("12.123456")).toBe("12.123456");
    expect(readPrinted("12345678901234")).toBe("12345678901234");
  });

  it("refuses what is not one amount", () => {
    for (const text of ["1,20", "12.3.4", "abc", "1e5", "", "$", "1,2345", "$12$", "(-12)", "12."]) {
      expect(readPrinted(text)).toBeNull();
    }
  });

  it("refuses what the server refuses", () => {
    // The server's `read_printed` allows plain spaces around the sign, the
    // symbol and the brackets only, and no more digits than the ledger holds.
    for (const text of [
      "1 204.18 €",
      "1\u00a0204.18",
      "12\t000",
      "\t12",
      "1.1234567",
      "123456789012345",
      "(12",
      "12)",
      "--12",
      "-$-12",
    ]) {
      expect(readPrinted(text)).toBeNull();
    }
  });
});

describe("liveDifference", () => {
  it("reads thousands separators and one currency symbol", () => {
    expect(liveDifference("1,204.18", "1,104.18", "-100.00", "ledger")).toEqual({
      text: "0.00",
      state: "balanced",
    });
    expect(liveDifference("$1,204.18", "$1,104.18", "-100.00", "ledger").state).toBe("balanced");
  });

  it("gives opening plus the rows less closing", () => {
    expect(liveDifference("500.00", "434.50", "-77.50", "ledger")).toEqual({
      text: "-12.00",
      state: "off",
    });
  });

  it("negates owed balances before the sum", () => {
    // A card: 100.00 owed, 50.00 of spending, 150.00 owed.
    expect(liveDifference("100.00", "150.00", "-50.00", "owed")).toEqual({
      text: "0.00",
      state: "balanced",
    });
    expect(liveDifference("100.00", "150.00", "-50.00", "ledger").state).toBe("off");
  });

  it("is invalid for a balance that is not a number", () => {
    expect(liveDifference("12.3.4", "10.00", "0.00", "ledger")).toEqual({
      text: "",
      state: "invalid",
    });
  });

  it("is unchecked until both balances are typed", () => {
    expect(liveDifference("", "10.00", "0.00", "ledger").state).toBe("unchecked");
    expect(liveDifference("10.00", "  ", "0.00", "ledger").state).toBe("unchecked");
  });

  it("is balanced within half a cent and off beyond it", () => {
    expect(liveDifference("100.00", "110.004", "10.00", "ledger").state).toBe("balanced");
    expect(liveDifference("100.00", "110.005", "10.00", "ledger").state).toBe("balanced");
    expect(liveDifference("100.00", "110.006", "10.00", "ledger").state).toBe("off");
    expect(liveDifference("100.00", "110.01", "10.00", "ledger").state).toBe("off");
  });

  it("adds without floating point", () => {
    expect(liveDifference("0.10", "0.30", "0.20", "ledger")).toEqual({
      text: "0.00",
      state: "balanced",
    });
  });
});

describe("expectedClosing", () => {
  it("is opening plus the rows, in the terms the statement prints", () => {
    expect(expectedClosing("500.00", "-77.50", "ledger")).toBe("422.50");
    expect(expectedClosing("100.00", "-50.00", "owed")).toBe("150.00");
    expect(expectedClosing("", "-50.00", "owed")).toBeNull();
    expect(expectedClosing("x", "-50.00", "owed")).toBeNull();
  });
});

describe("causeText", () => {
  it("has its own sentence for every kind of cause", () => {
    const seen = new Set<string>();
    for (const kind of CAUSE_KINDS) {
      const text = causeText(cause(kind), "USD");
      expect(text.length).toBeGreaterThan(20);
      expect(seen.has(text)).toBe(false);
      seen.add(text);
    }
    expect(seen.size).toBe(9);
  });

  it("uses the words of the plan", () => {
    expect(causeText(cause("extra_row"), "USD")).toBe(
      "One row is exactly the difference. It may be a row too many, such as a total or a balance line read as a transaction.",
    );
    expect(causeText(cause("sign_flipped"), "USD")).toBe(
      "The difference is twice this row. Its sign may be the wrong way round.",
    );
    expect(causeText(cause("transposed_digits"), "USD")).toBe(
      "The difference divides by 9. Two digits of an amount may be swapped.",
    );
    expect(causeText(cause("fee_or_interest"), "USD")).toBe(
      "A fee or interest line on the statement has the size of the difference and was not read as a row.",
    );
    expect(causeText(cause("opening_differs_from_previous", "480.00"), "USD")).toBe(
      "The opening balance is not the closing balance of the previous statement ($480.00).",
    );
    expect(causeText(cause("duplicated_row"), "USD")).toBe(
      "These rows have the same date and amount. One may be a duplicate.",
    );
    expect(causeText(cause("out_of_period"), "USD")).toBe(
      "These rows are dated outside the statement period.",
    );
    expect(causeText(cause("earlier_reading"), "USD")).toBe(
      "These rows come from an earlier reading of this file. They were kept because they had been changed.",
    );
    expect(causeText(cause("missing_row", "-12.00"), "USD")).toBe(
      "No row explains the difference. A row of -$12.00 may be missing.",
    );
  });

  it("leaves the amount out when the server sends none", () => {
    expect(causeText(cause("missing_row", null), "USD")).toBe(
      "No row explains the difference. A row may be missing.",
    );
    expect(causeText(cause("opening_differs_from_previous", null), "USD")).toBe(
      "The opening balance is not the closing balance of the previous statement.",
    );
  });
});

describe("statusLine", () => {
  it("says balanced, off by how much, or that nothing can be checked", () => {
    expect(statusLine(statement({ status: "balanced", difference: "0.00" }))).toBe("Balanced.");
    expect(statusLine(statement())).toBe("Off by $12.00.");
    expect(
      statusLine(statement({ status: "unchecked", difference: null, opening: null })),
    ).toBe("No balances to check against. Enter them as printed on the statement.");
  });
});

describe("teachHint", () => {
  const HINT =
    "PerFin read several rows of this layout wrongly. Teach it the layout so the next statement reads right.";

  it("appears with three or more of either correction", () => {
    expect(teachHint({ flipped: 0, removed: 0 })).toBeNull();
    expect(teachHint({ flipped: 2, removed: 2 })).toBeNull();
    expect(teachHint({ flipped: 3, removed: 0 })).toBe(HINT);
    expect(teachHint({ flipped: 0, removed: 3 })).toBe(HINT);
    expect(teachHint({ flipped: 5, removed: 4 })).toBe(HINT);
  });
});

describe("skippedText", () => {
  it("counts the lines that were not read", () => {
    expect(skippedText(0, true)).toBeNull();
    expect(skippedText(1, true)).toBe(
      "1 line of the statement was not read. It is marked on the page.",
    );
    expect(skippedText(4, true)).toBe(
      "4 lines of the statement were not read. They are marked on the page.",
    );
  });

  it("does not say a line is marked when no box is drawn for it", () => {
    expect(skippedText(1, false)).toBe("1 line of the statement was not read.");
    expect(skippedText(4, false)).toBe("4 lines of the statement were not read.");
  });
});

describe("statementToCheck", () => {
  const settled = statement({
    statement: 0,
    status: "balanced",
    verification: "reconciled",
  });
  const savings = statement({ statement: 1, account_id: "a-2" });
  const third = statement({ statement: 2, account_id: "a-3" });

  it("is the statement that was asked for", () => {
    expect(statementToCheck([settled, savings], 0, null)).toBe(settled);
    expect(statementToCheck([settled, savings], 1, null)).toBe(savings);
    expect(statementToCheck([settled, savings], 5, null)).toBeUndefined();
  });

  it("is the first statement still in doubt when none was named", () => {
    expect(statementToCheck([settled, savings, third], null, null)).toBe(savings);
  });

  it("prefers the statement of the account the finding names", () => {
    expect(statementToCheck([settled, savings, third], null, "a-3")).toBe(third);
    expect(statementToCheck([settled, savings, third], null, "a-9")).toBe(savings);
  });

  it("is the first statement when every one is settled", () => {
    expect(statementToCheck([settled], null, null)).toBe(settled);
    expect(statementToCheck([], null, null)).toBeUndefined();
  });
});
