// SPDX-License-Identifier: AGPL-3.0-or-later
import { account } from "../transactions/fixtures";
import { freshness, groupAccounts, groupOf, GROUPS, groupTotal } from "./groups";

const NOW = new Date("2026-11-16T12:00:00Z");

describe("account groups", () => {
  it("puts each type in its group", () => {
    const expected = new Map([
      ["checking", "cash"],
      ["savings", "cash"],
      ["cash", "cash"],
      ["credit_card", "credit"],
      ["line_of_credit", "credit"],
      ["brokerage", "investments"],
      ["retirement", "investments"],
      ["loan", "loans"],
      ["mortgage", "loans"],
      ["manual_liability", "loans"],
      ["manual_asset", "property"],
    ] as const);
    for (const [type, group] of expected) {
      expect(groupOf(type)).toBe(group);
    }
    expect(GROUPS.map((group) => group.id)).toEqual(["cash", "credit", "investments", "loans", "property"]);
    expect(GROUPS.flatMap((group) => group.types)).toHaveLength(expected.size);
  });

  it("groups in a fixed order with decimal totals, leaving out closed accounts and empty groups", () => {
    const groups = groupAccounts([
      account("a-1", "Everyday", { balance: { amount: "1200.50", currency: "USD" } }),
      account("a-2", "Rainy day", { type: "savings", balance: { amount: "5000.25", currency: "USD" } }),
      account("a-3", "Card", { type: "credit_card", balance: { amount: "-300.10", currency: "USD" } }),
      account("a-4", "Old", { is_closed: true, closed_on: "2026-01-01", balance: { amount: "9.00", currency: "USD" } }),
      account("a-5", "Home", { type: "manual_asset", balance: { amount: "250000", currency: "USD" } }),
    ]);
    expect(groups.map((group) => [group.id, group.label])).toEqual([
      ["cash", "Cash"],
      ["credit", "Cards and credit"],
      ["property", "Property and other"],
    ]);
    expect(groups[0]?.total).toEqual({ amount: "6200.75", currency: "USD" });
    expect(groups[0]?.accounts.map((item) => item.name)).toEqual(["Everyday", "Rainy day"]);
    expect(groups[1]?.total).toEqual({ amount: "-300.10", currency: "USD" });
  });

  it("gives no total when a group mixes currencies", () => {
    expect(
      groupTotal([account("x", "x"), account("y", "y", { balance: { amount: "1.00", currency: "EUR" } })]),
    ).toBeNull();
    expect(groupTotal([])).toBeNull();
  });

  it("says how current a balance is, and that a manual one is an estimate", () => {
    expect(freshness(account("a-1", "Everyday", { balance_date: "2026-11-12" }), NOW)).toBe("Balance from Nov 12");
    expect(freshness(account("a-1", "Everyday", { balance_date: "2025-12-30" }), NOW)).toBe("Balance from Dec 30, 2025");
    expect(
      freshness(account("a-2", "Home", { type: "manual_asset", balance_date: "2026-06-01" }), NOW),
    ).toBe("Your estimate, June 2026");
    expect(freshness(account("a-3", "Cash box", { manual_balance: true, balance_date: "2026-10-02" }), NOW)).toBe(
      "Your estimate, October 2026",
    );
  });
});
