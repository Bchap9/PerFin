// SPDX-License-Identifier: AGPL-3.0-or-later
/** The Inbox's suggested categories by merchant (`groupSuggestions`). */
import { tx } from "../transactions/fixtures";
import { groupSuggestions } from "./cards";

const suggested = (id: string, raw: string | null, extra: Parameters<typeof tx>[1] = {}) =>
  tx(id, { category_id: null, suggested_category_id: "c-2", raw_payee: raw, ...extra });

describe("groupSuggestions", () => {
  it("groups rows with no merchant by their printed text", () => {
    const groups = groupSuggestions([suggested("t-1", "EXAMPLE SHOP 1"), suggested("t-2", "EXAMPLE SHOP 1")]);
    expect(groups.map((group) => group.ids)).toEqual([["t-1", "t-2"]]);
  });

  it("groups rows by merchant before their printed text", () => {
    const groups = groupSuggestions([
      suggested("t-1", "EXAMPLE SHOP 1", { merchant_key: "example shop" }),
      suggested("t-2", "EXAMPLE SHOP 2", { merchant_key: "example shop" }),
    ]);
    expect(groups.map((group) => group.ids)).toEqual([["t-1", "t-2"]]);
  });

  it("never groups rows whose text has fewer than three letters and digits", () => {
    const groups = groupSuggestions([
      suggested("t-1", "#1"),
      suggested("t-2", "#1"),
      suggested("t-3", ""),
      suggested("t-4", null),
      suggested("t-5", " - * "),
      suggested("t-6", " - * "),
    ], 10);
    expect(groups.map((group) => group.ids).sort()).toEqual([["t-1"], ["t-2"], ["t-3"], ["t-4"], ["t-5"], ["t-6"]]);
  });

  it("still groups short text of three letters and digits", () => {
    const groups = groupSuggestions([suggested("t-1", "A-1 B"), suggested("t-2", "A-1 B")]);
    expect(groups.map((group) => group.ids)).toEqual([["t-1", "t-2"]]);
  });
});
