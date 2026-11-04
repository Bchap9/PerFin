// SPDX-License-Identifier: AGPL-3.0-or-later
import type { NetWorthHistory, NetWorthLatest, NetWorthProjection } from "./api";
import { accountRows, classLabel, classRows, historyPoints, projectionPoints } from "./chartData";

const m = (amount: string) => ({ amount, currency: "USD" });

describe("net worth chart data", () => {
  it("maps history buckets to exact amounts plus display-only plot values", () => {
    const history = {
      currency: "USD",
      range: "1y",
      granularity: "month",
      scope: "household",
      date_from: "2026-01-31",
      date_to: "2026-02-28",
      change: m("1100.10"),
      points: [
        { as_of: "2026-01-31", assets: m("10000.10"), liabilities: m("4000.00"), net: m("6000.10"), source: "snapshot" },
        { as_of: "2026-02-28", assets: m("11000.20"), liabilities: m("3900.00"), net: m("7100.20"), source: "computed" },
      ],
    } satisfies NetWorthHistory;
    const points = historyPoints(history);
    expect(points).toHaveLength(2);
    expect(points[0]).toEqual({
      date: "2026-01-31",
      amounts: { assets: "10000.10", liabilities: "4000.00", net: "6000.10" },
      plot: { assets: 10000.1, liabilities: 4000, net: 6000.1 },
    });
    expect(points[1]?.amounts["net"]).toBe("7100.20");
  });

  it("merges the trend and savings projections on one date axis", () => {
    const projection = {
      as_of: "2026-11-03",
      currency: "USD",
      scope: "household",
      months: 2,
      basis_months: 12,
      current: m("100.00"),
      trend: { slope_per_month: m("10.00"), points: [{ on: "2026-12-03", net: m("110.00") }, { on: "2027-01-03", net: m("120.00") }] },
      savings: { average_monthly_savings: m("5.00"), savings_rate: "0.05", points: [{ on: "2026-12-03", net: m("105.00") }] },
      unconverted: [],
    } satisfies NetWorthProjection;
    const points = projectionPoints(projection);
    expect(points.map((p) => p.date)).toEqual(["2026-11-03", "2026-12-03", "2027-01-03"]);
    expect(points[0]?.amounts).toEqual({ trend: "100.00", savings: "100.00" });
    expect(points[1]?.amounts).toEqual({ trend: "110.00", savings: "105.00" });
    expect(points[2]?.amounts).toEqual({ trend: "120.00" });
  });

  it("builds breakdown rows: zero classes dropped, labels readable, liabilities as owed", () => {
    expect(classLabel("real_estate")).toBe("Real estate");
    expect(classLabel("odd_one")).toBe("odd one");
    expect(classRows({ cash: m("5.00"), vehicles: m("0.00") })).toEqual([{ key: "cash", label: "Cash", amount: "5.00", currency: "USD" }]);
    const latest = {
      as_of: "2026-11-03",
      currency: "USD",
      scope: "visible_accounts",
      assets: m("5"),
      liabilities: m("100"),
      net: m("-95"),
      by_class: { assets: {}, liabilities: {} },
      by_type: {},
      unconverted: [],
      unpriced_security_ids: [],
      last_snapshot_date: null,
      accounts: [
        { account_id: "a", name: "Checking", type: "checking", account_class: "cash", side: "asset", amount: m("5"), amount_base: m("5") },
        { account_id: "b", name: "Card", type: "credit_card", account_class: "credit_cards", side: "liability", amount: m("-100"), amount_base: m("-100") },
        { account_id: "c", name: "Euro", type: "savings", account_class: "cash", side: "asset", amount: { amount: "9", currency: "EUR" }, amount_base: null },
      ],
    } satisfies NetWorthLatest;
    expect(accountRows(latest, "asset").map((r) => r.label)).toEqual(["Checking"]);
    expect(accountRows(latest, "liability")).toEqual([{ key: "b", label: "Card", amount: "100", currency: "USD" }]);
  });
});
