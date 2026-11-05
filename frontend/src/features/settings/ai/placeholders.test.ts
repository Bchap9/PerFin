// SPDX-License-Identifier: AGPL-3.0-or-later
import { countPlaceholders, PLACEHOLDERS, splitPlaceholders } from "./placeholders";

describe("splitPlaceholders", () => {
  it("returns plain text as one segment", () => {
    expect(splitPlaceholders("COFFEE SHOP")).toEqual([{ kind: "text", text: "COFFEE SHOP" }]);
  });

  it("returns nothing for an empty string", () => {
    expect(splitPlaceholders("")).toEqual([]);
  });

  it("separates every kind of placeholder and joins back to the original", () => {
    const text = PLACEHOLDERS.map((placeholder) => `x ${placeholder} y`).join(" | ");
    const segments = splitPlaceholders(text);
    expect(segments.filter((segment) => segment.kind === "placeholder").map((segment) => segment.text)).toEqual([...PLACEHOLDERS]);
    expect(segments.map((segment) => segment.text).join("")).toBe(text);
  });

  it("handles adjacent and repeated placeholders", () => {
    expect(splitPlaceholders("[ACCT][ACCT]")).toEqual([
      { kind: "placeholder", text: "[ACCT]" },
      { kind: "placeholder", text: "[ACCT]" },
    ]);
  });

  it("does not treat lookalikes as placeholders", () => {
    for (const text of ["[acct]", "[ACCT", "ACCT]", "[ ACCT ]", "[ACCT ]"]) {
      expect(splitPlaceholders(text)).toEqual([{ kind: "text", text }]);
    }
  });

  it("keeps markup as plain text", () => {
    const evil = '<img src=x onerror="alert(1)">[EMAIL]</b>';
    const segments = splitPlaceholders(evil);
    expect(segments).toEqual([
      { kind: "text", text: '<img src=x onerror="alert(1)">' },
      { kind: "placeholder", text: "[EMAIL]" },
      { kind: "text", text: "</b>" },
    ]);
  });
});

describe("countPlaceholders", () => {
  it("counts each kind", () => {
    expect(countPlaceholders("[ACCT] and [ACCT] and [PHONE] and plain")).toEqual({ "[ACCT]": 2, "[PHONE]": 1 });
  });
});
