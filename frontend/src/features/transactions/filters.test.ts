// SPDX-License-Identifier: AGPL-3.0-or-later
import { tx } from "./fixtures";
import { fromSearch, hasFilters, NO_FILTERS, toSearch, totals, withFilters } from "./filters";

describe("filters in the address", () => {
  it("round trips every key and leaves the defaults out", () => {
    const search = {
      account: "a-1",
      category: "c-2",
      suggested: "true",
      merchant: "quiet bean coffee",
      subscription: "s-1",
      document: "d-7",
      date_from: "2026-10-01",
      date_to: "2026-10-31",
      q: "coffee",
      payee: "p-1",
      tag: "t-1",
      review_state: "pending",
    };
    const filters = fromSearch(search);
    expect(filters).toEqual({
      account: "a-1",
      category: "c-2",
      suggested: true,
      merchant: "quiet bean coffee",
      subscription: "s-1",
      document: "d-7",
      dateFrom: "2026-10-01",
      dateTo: "2026-10-31",
      q: "coffee",
      payee: "p-1",
      tag: "t-1",
      reviewState: "pending",
    });
    expect(toSearch(filters)).toEqual(search);
    expect(fromSearch(toSearch(filters))).toEqual(filters);
    // Nothing set, nothing written.
    expect(toSearch(NO_FILTERS)).toEqual({});
    expect(fromSearch({})).toEqual(NO_FILTERS);
    expect(hasFilters(NO_FILTERS)).toBe(false);
    expect(hasFilters(filters)).toBe(true);
  });

  it("ignores unknown keys and values it cannot use", () => {
    const filters = fromSearch({
      date_from: "last week",
      date_to: "2026-02-30",
      review_state: "maybe",
      suggested: "yes",
      account: "",
      sort: "amount",
      tx: "t-1",
    });
    expect(filters).toEqual(NO_FILTERS);
    expect(hasFilters(filters)).toBe(false);
  });

  it("reads the router's typed values as text", () => {
    // The router parses `?q=2024&suggested=true` as JSON: a number and a boolean.
    expect(fromSearch({ q: 2024, suggested: true })).toMatchObject({ q: "2024", suggested: true });
  });

  it("category=none is the uncategorised filter", () => {
    expect(fromSearch({ category: "none" }).category).toBe("none");
    expect(toSearch({ ...NO_FILTERS, category: "none" })).toEqual({ category: "none" });
  });

  it("writes the filters over the address and keeps what is not a filter", () => {
    const next = withFilters({ account: "a-1", q: "old", tx: "t-1", always: "t-2" }, { ...NO_FILTERS, suggested: true, q: "new" });
    expect(next).toEqual({ tx: "t-1", always: "t-2", suggested: true, q: "new" });
  });
});

describe("totals of the rows shown", () => {
  it("adds money in and money out, refunds included, in decimal strings", () => {
    const rows = [
      tx("a", { amount: { amount: "-42.10", currency: "USD" } }),
      tx("b", { amount: { amount: "-0.20", currency: "USD" } }),
      // A refund is money in.
      tx("c", { amount: { amount: "12.30", currency: "USD" } }),
      tx("d", { amount: { amount: "1000", currency: "USD" } }),
    ];
    expect(totals(rows)).toEqual({ in: "1012.30", out: "42.30", net: "970.00", count: 4, transfers: 0, currency: "USD" });
  });

  it("counts a transfer in neither in nor out, and says how many were left out", () => {
    const rows = [
      tx("a", { amount: { amount: "-50.00", currency: "USD" } }),
      tx("b", { amount: { amount: "-500.00", currency: "USD" }, transfer: { group_id: "g-1", counterpart_id: "x-9" } }),
      tx("c", { amount: { amount: "500.00", currency: "USD" }, transfer: { group_id: "g-1", counterpart_id: "b" } }),
    ];
    expect(totals(rows)).toEqual({ in: "0", out: "50.00", net: "-50.00", count: 3, transfers: 2, currency: "USD" });
  });

  it("does not add amounts of different currencies", () => {
    const rows = [tx("a", { amount: { amount: "-5.00", currency: "USD" } }), tx("b", { amount: { amount: "-5.00", currency: "EUR" } })];
    expect(totals(rows)).toMatchObject({ count: 2, currency: null });
  });

  it("says nothing is shown for no rows", () => {
    expect(totals([])).toEqual({ in: "0", out: "0", net: "0", count: 0, transfers: 0, currency: null });
  });
});
