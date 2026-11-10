// SPDX-License-Identifier: AGPL-3.0-or-later
import { CAT_RENT, money } from "../planning/testing";
import {
  allZero,
  cashflowBars,
  formatPercent,
  groupNoun,
  itemLabel,
  monthBars,
  monthLabel,
  monthTick,
  rankedBars,
  REST_KEY,
  sharePercent,
  trendBars,
  valueAxis,
} from "./chartData";
import { reportKeys } from "./api";
import { transactionsSearch } from "./links";
import { parseReportSearch, reportSearch, DEFAULT_STATE } from "./searchState";
import { cashflowReport, flowReport, trendReport } from "./testing";

describe("report labels", () => {
  it("names rows the server left unnamed", () => {
    expect(itemLabel({ key: CAT_RENT, name: "Rent" }, "category")).toBe("Rent");
    expect(itemLabel({ key: null, name: null }, "category")).toBe("Uncategorised");
    expect(itemLabel({ key: CAT_RENT, name: null }, "category")).toBe("Unknown category");
    expect(itemLabel({ key: null, name: null }, "payee")).toBe("No payee");
    expect(itemLabel({ key: "p-1", name: null }, "payee")).toBe("Unknown payee");
    expect(itemLabel({ key: "a-1", name: null }, "account")).toBe("Unknown account");
    expect(itemLabel({ key: "2026-11", name: null }, "month")).toBe(monthLabel("2026-11"));
  });

  it("labels months, with the year only where an axis needs it", () => {
    expect(monthLabel("2026-11")).toBe("November 2026");
    expect(monthLabel("2026-11", "short")).toBe("Nov 2026");
    expect(monthLabel("not-a-month")).toBe("not-a-month");
    const first = "2025-11";
    expect(monthTick("2025-11", first)).toBe(monthLabel("2025-11", "short"));
    expect(monthTick("2026-01", first)).toBe(monthLabel("2026-01", "short"));
    expect(monthTick("2025-12", first)).toBe("Dec");
  });

  it("counts groups in words", () => {
    expect(groupNoun("category", 1)).toBe("category");
    expect(groupNoun("category", 2)).toBe("categories");
    expect(groupNoun("payee", 5)).toBe("payees");
  });
});

describe("chart data", () => {
  it("keeps the exact amount next to each plotted number", () => {
    expect(rankedBars(flowReport())).toEqual([
      { key: CAT_RENT, label: "Rent", amount: "1450.00", plot: 1450 },
      expect.objectContaining({ label: "Food", amount: "300.50", plot: 300.5 }),
      { key: "none-2", label: "Uncategorised", amount: "49.50", plot: 49.5 },
    ]);
  });

  it("adds the smallest groups up into one bar past the limit", () => {
    const items = Array.from({ length: 15 }, (_, index) => ({ key: `p-${String(index)}`, name: `Payee ${String(index)}`, amount: money(`${String(100 - index)}.10`) }));
    const bars = rankedBars({ group_by: "payee", items }, 12);
    expect(bars).toHaveLength(12);
    expect(bars[10]?.label).toBe("Payee 10");
    // 89.10 + 88.10 + 87.10 + 86.10, added exactly.
    expect(bars[11]).toEqual({ key: REST_KEY, label: "4 smaller payees", amount: "350.40", plot: 350.4 });
    expect(rankedBars({ group_by: "payee", items: items.slice(0, 12) }, 12)).toHaveLength(12);
  });

  it("maps months, cash flow and trends", () => {
    const byMonth = flowReport({
      group_by: "month",
      items: [
        { key: "2026-10", name: null, amount: money("10.00") },
        { key: "2026-11", name: null, amount: money("-2.00") },
      ],
    });
    expect(monthBars(byMonth).map((bar) => [bar.month, bar.amount, bar.plot, bar.role])).toEqual([
      ["2026-10", "10.00", 10, "other"],
      ["2026-11", "-2.00", -2, "other"],
    ]);

    expect(cashflowBars(cashflowReport())[0]).toEqual({
      month: "2026-10",
      label: monthLabel("2026-10"),
      inflow: "3466.18",
      outflow: "3667.08",
      net: "-200.90",
      plotIn: 3466.18,
      plotOut: 3667.08,
      plotNet: -200.9,
    });

    const series = ["2025-11", "2025-12", "2026-09", "2026-10", "2026-11"].map((month) => ({ month, amount: money("1.00") }));
    expect(trendBars(trendReport({ series })).map((bar) => bar.role)).toEqual(["year-ago", "other", "other", "previous", "current"]);
  });

  it("knows when there is nothing to draw", () => {
    expect(allZero([])).toBe(true);
    expect(allZero(["0.00", "0", "-0.00"])).toBe(true);
    expect(allZero(["0.00", "0.01"])).toBe(false);
  });

  it("computes shares without floats", () => {
    expect(sharePercent("1450.00", "1800.00")).toBe("80.6");
    expect(sharePercent("300.50", "1800.00")).toBe("16.7");
    expect(sharePercent("1", "3")).toBe("33.3");
    expect(sharePercent("2", "3")).toBe("66.7");
    expect(sharePercent("-2.50", "7.50")).toBe("-33.3");
    expect(sharePercent("5", "5.000")).toBe("100.0");
    expect(sharePercent("0.00", "10")).toBe("0.0");
    expect(sharePercent("5", "0.00")).toBeNull();
    expect(sharePercent("abc", "10")).toBeNull();
    // Too large for a double, still exact.
    expect(sharePercent("900719925474099300.01", "1801439850948198600.02")).toBe("50.0");
  });

  it("formats percentages", () => {
    expect(formatPercent("62.4")).toBe("62.4%");
    expect(formatPercent("4524.18", { signed: true })).toBe("+4,524.2%");
    expect(formatPercent("-36.64", { signed: true })).toBe("-36.6%");
    expect(formatPercent("0.00", { signed: true })).toBe("0.0%");
  });

  it("picks clean axis values that cover zero and the data", () => {
    expect(valueAxis([3696.29, 3304.5])).toEqual({ ticks: [0, 1000, 2000, 3000, 4000], whole: true });
    expect(valueAxis([4350, 2157.92])).toEqual({ ticks: [0, 1000, 2000, 3000, 4000, 5000], whole: true });
    expect(valueAxis([1450, 499.09])).toEqual({ ticks: [0, 500, 1000, 1500], whole: true });
    expect(valueAxis([3773.62, -2323.59])).toEqual({ ticks: [-4000, -2000, 0, 2000, 4000], whole: true });
    expect(valueAxis([-120, -40])).toEqual({ ticks: [-125, -100, -75, -50, -25, 0], whole: true });
    expect(valueAxis([0.9, 0.35])).toEqual({ ticks: [0, 0.2, 0.4, 0.6, 0.8, 1], whole: false });
    expect(valueAxis([])).toEqual({ ticks: [0, 1], whole: true });
    expect(valueAxis([0, 0])).toEqual({ ticks: [0, 1], whole: true });
    const wide = valueAxis([123456789.12]);
    expect(wide.ticks[0]).toBe(0);
    expect(wide.ticks.at(-1)).toBeGreaterThanOrEqual(123456789.12);
    expect(wide.ticks.length).toBeLessThanOrEqual(6);
  });
});

describe("report links", () => {
  const report = { date_from: "2026-11-01", date_to: "2026-11-30" };

  it("sends a category row to Transactions with the filters it has", () => {
    expect(transactionsSearch({ ...report, group_by: "category" }, { key: CAT_RENT }, "")).toEqual({ category: CAT_RENT, date_from: "2026-11-01", date_to: "2026-11-30" });
    expect(transactionsSearch({ ...report, group_by: "category" }, { key: null }, "a-1")).toEqual({
      account: "a-1",
      category: "none",
      date_from: "2026-11-01",
      date_to: "2026-11-30",
    });
  });

  it("sends an account row to that account, and links nothing else", () => {
    expect(transactionsSearch({ ...report, group_by: "account" }, { key: "a-2" }, "a-1")).toEqual({ account: "a-2", date_from: "2026-11-01", date_to: "2026-11-30" });
    expect(transactionsSearch({ ...report, group_by: "account" }, { key: null }, "")).toBeNull();
    expect(transactionsSearch({ ...report, group_by: "payee" }, { key: "p-1" }, "")).toBeNull();
    expect(transactionsSearch({ ...report, group_by: "month" }, { key: "2026-11" }, "")).toBeNull();
  });
});

describe("report page state", () => {
  it("starts from the defaults and leaves them out of the address", () => {
    expect(parseReportSearch({})).toEqual(DEFAULT_STATE);
    expect(reportSearch(DEFAULT_STATE)).toEqual({});
  });

  it("round-trips a full state", () => {
    const state = {
      tab: "trends",
      period: "custom",
      from: "2026-01-01",
      to: "2026-03-31",
      account: "a-1",
      pending: true,
      group: "payee",
      depth: 2,
      flow: "income",
      months: 25,
    } as const;
    const search = reportSearch(state);
    expect(search).toEqual({ tab: "trends", period: "custom", from: "2026-01-01", to: "2026-03-31", account: "a-1", pending: true, group: "payee", depth: 2, flow: "income", months: 25 });
    expect(parseReportSearch(search)).toEqual(state);
    // Values typed into the address arrive as strings.
    expect(parseReportSearch({ ...search, pending: "false", depth: "3", months: "60" })).toEqual({ ...state, pending: false, depth: 3, months: 60 });
  });

  it("ignores anything it does not know", () => {
    expect(
      parseReportSearch({ tab: "nope", period: "decade", from: "2026-13-01", to: 5, account: { id: 1 }, pending: "yes", group: "tag", depth: 9, flow: "both", months: 1000 }),
    ).toEqual(DEFAULT_STATE);
    expect(parseReportSearch({ account: "x".repeat(65) }).account).toBe("");
  });

  it("drops custom dates when a preset is chosen", () => {
    expect(reportSearch({ ...DEFAULT_STATE, period: "last-month", from: "2026-01-01", to: "2026-01-31" })).toEqual({ period: "last-month" });
  });
});

describe("report query keys", () => {
  const scope = { account: "", pending: null };

  it("sit under the reports root so live updates refresh them", () => {
    const flow = { flow: "spending", from: "2026-11-01", to: "2026-11-30", groupBy: "category", depth: 2, ...scope } as const;
    expect(reportKeys.flow(flow)[0]).toBe("reports");
    expect(reportKeys.cashflow({ from: "2026-11-01", to: "2026-11-30", ...scope })[0]).toBe("reports");
    expect(reportKeys.trends({ flow: "income", month: "2026-11", months: 13, depth: 1, ...scope })[0]).toBe("reports");
    // Spending and income, and different filters, never share an entry.
    expect(reportKeys.flow(flow)).not.toEqual(reportKeys.flow({ ...flow, flow: "income" }));
    expect(reportKeys.flow(flow)).not.toEqual(reportKeys.flow({ ...flow, pending: true }));
    expect(reportKeys.flow(flow)).not.toEqual(reportKeys.flow({ ...flow, account: "a-1" }));
  });

  it("ignore the category level when it has no effect", () => {
    const byPayee = { flow: "spending", from: "2026-11-01", to: "2026-11-30", groupBy: "payee", depth: 1, ...scope } as const;
    expect(reportKeys.flow(byPayee)).toEqual(reportKeys.flow({ ...byPayee, depth: 3 }));
  });
});
