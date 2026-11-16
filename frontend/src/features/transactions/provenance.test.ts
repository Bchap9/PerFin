// SPDX-License-Identifier: AGPL-3.0-or-later
import { tx } from "./fixtures";
import { confidenceText, isGuess, isSuggestion, sourceLabel } from "./provenance";

describe("sourceLabel", () => {
  it("says in words how each category was decided", () => {
    expect(sourceLabel(tx("a", { category_source: "person" }))).toBe("You chose this");
    expect(sourceLabel(tx("a", { category_source: "file" }))).toBe("From the file");
    expect(sourceLabel(tx("a", { category_source: "payee" }))).toBe("Payee default");
    expect(sourceLabel(tx("a", { category_source: "learned" }))).toBe("Learned from you");
    expect(sourceLabel(tx("a", { category_source: "dictionary" }))).toBe("Merchant list");
    expect(sourceLabel(tx("a", { category_source: "keyword" }))).toBe("Keyword");
  });

  it("names the rule, or says that it is gone", () => {
    const row = tx("a", { category_source: "rule", category_rule_id: "r-1" });
    expect(sourceLabel(row, "Coffee")).toBe("Rule: Coffee");
    expect(sourceLabel(row)).toBe("A rule that no longer exists");
  });

  it("says nothing about a row from before provenance, or an undecided one", () => {
    expect(sourceLabel(tx("a", { category_source: "legacy" }))).toBeNull();
    expect(sourceLabel(tx("a", { category_source: null }))).toBeNull();
    expect(sourceLabel(tx("a"))).toBeNull();
  });
});

describe("confidenceText", () => {
  it("turns decimal text into a whole percentage", () => {
    expect(confidenceText("0.90")).toBe("90%");
    expect(confidenceText("0.55")).toBe("55%");
    expect(confidenceText("0.60")).toBe("60%");
    expect(confidenceText("1.00")).toBe("100%");
  });

  it("is silent without a usable number", () => {
    expect(confidenceText(null)).toBeNull();
    expect(confidenceText(undefined)).toBeNull();
    expect(confidenceText("")).toBeNull();
    expect(confidenceText("sure")).toBeNull();
    expect(confidenceText("1.50")).toBeNull();
  });
});

describe("isGuess", () => {
  it("is true for what PerFin worked out and false for what somebody decided", () => {
    expect(["learned", "dictionary", "keyword"].map((source) => isGuess(source as "learned"))).toEqual([true, true, true]);
    expect(["person", "file", "rule", "payee", "legacy"].map((source) => isGuess(source as "person"))).toEqual([false, false, false, false, false]);
    expect(isGuess(null)).toBe(false);
    expect(isGuess(undefined)).toBe(false);
  });
});

describe("isSuggestion", () => {
  it("is a row with a suggestion and no category", () => {
    expect(isSuggestion(tx("a", { category_id: null, suggested_category_id: "c-2" }))).toBe(true);
  });

  it("is not a row with a category, without a suggestion, or with a split", () => {
    expect(isSuggestion(tx("a", { category_id: "c-1", suggested_category_id: "c-2" }))).toBe(false);
    expect(isSuggestion(tx("a", { category_id: null, suggested_category_id: null }))).toBe(false);
    expect(isSuggestion(tx("a", { category_id: null }))).toBe(false);
    expect(
      isSuggestion(
        tx("a", {
          category_id: null,
          suggested_category_id: "c-2",
          splits: [{ id: "s-1", amount: { amount: "-1.00", currency: "USD" }, category_id: "c-1", memo: null, tax_flag: false }],
        }),
      ),
    ).toBe(false);
  });
});
