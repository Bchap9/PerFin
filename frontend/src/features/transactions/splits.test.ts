// SPDX-License-Identifier: AGPL-3.0-or-later
import { normalizeAmountInput, suggestedRemainder, validateSplits, type SplitDraft } from "./splits";

const line = (amount: string, key = amount): SplitDraft => ({ key, amount, categoryId: "", memo: "" });

describe("split validation", () => {
  it("accepts lines that add up exactly, with no float drift", () => {
    expect(validateSplits("-0.30", [line("-0.10", "a"), line("-0.20", "b")])).toMatchObject({ ok: true, remainder: "0.00", formError: null });
    // 0.1 + 0.2 !== 0.3 in floating point; decimal strings are exact.
    expect(validateSplits("0.3", [line("0.1", "a"), line("0.2", "b")]).ok).toBe(true);
    expect(validateSplits("1234567890123.45", [line("1234567890123.40", "a"), line("0.05", "b")]).ok).toBe(true);
  });

  it("reports what is left when the lines do not add up", () => {
    const result = validateSplits("-42.10", [line("-40.00", "a"), line("-1.00", "b")]);
    expect(result.ok).toBe(false);
    expect(result.remainder).toBe("-1.10");
    expect(result.formError).toBe("The lines are off by -1.10.");
  });

  it("needs two lines and valid, non-zero amounts", () => {
    expect(validateSplits("10.00", [line("10.00")])).toMatchObject({ ok: false, formError: "A split needs at least two lines." });
    const bad = validateSplits("10.00", [line("abc", "a"), line("0", "b"), line("1e3", "c")]);
    expect(bad.ok).toBe(false);
    expect([...bad.lineErrors.keys()]).toEqual([0, 1, 2]);
    expect(bad.remainder).toBeNull();
  });

  it("normalises thousands separators and refuses non-decimals", () => {
    expect(normalizeAmountInput(" 1,234.50 ")).toBe("1234.50");
    expect(normalizeAmountInput("12.")).toBeNull();
    expect(normalizeAmountInput("")).toBeNull();
  });

  it("suggests the remaining amount for a new line", () => {
    expect(suggestedRemainder("-42.10", [line("-40.00"), line("")])).toBe("-2.10");
    expect(suggestedRemainder("10.00", [line("10.00")])).toBe("0.00");
  });
});
