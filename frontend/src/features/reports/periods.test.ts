// SPDX-License-Identifier: AGPL-3.0-or-later
import { describeRange, monthOf, presetRange, rangeProblem, rangeProblemText, resolvePeriod, shiftMonth } from "./periods";

describe("report periods", () => {
  it("resolves every preset to whole calendar months around today", () => {
    const today = "2026-11-10";
    expect(presetRange("this-month", today)).toEqual({ from: "2026-11-01", to: "2026-11-30" });
    expect(presetRange("last-month", today)).toEqual({ from: "2026-10-01", to: "2026-10-31" });
    expect(presetRange("last-3-months", today)).toEqual({ from: "2026-09-01", to: "2026-11-30" });
    expect(presetRange("year-to-date", today)).toEqual({ from: "2026-01-01", to: "2026-11-30" });
    expect(presetRange("last-year", today)).toEqual({ from: "2025-01-01", to: "2025-12-31" });
    // The server's own default for cash flow: this month and the eleven before.
    expect(presetRange("last-12-months", today)).toEqual({ from: "2025-12-01", to: "2026-11-30" });
  });

  it("handles year boundaries, leap years and month ends", () => {
    expect(presetRange("last-month", "2026-01-31")).toEqual({ from: "2025-12-01", to: "2025-12-31" });
    expect(presetRange("last-month", "2024-03-31")).toEqual({ from: "2024-02-01", to: "2024-02-29" });
    expect(presetRange("last-3-months", "2026-01-15")).toEqual({ from: "2025-11-01", to: "2026-01-31" });
    expect(presetRange("this-month", "2025-02-28")).toEqual({ from: "2025-02-01", to: "2025-02-28" });
    expect(presetRange("year-to-date", "2026-01-01")).toEqual({ from: "2026-01-01", to: "2026-01-31" });
  });

  it("checks a custom range the way the server does", () => {
    expect(rangeProblem("2026-01-01", "2026-01-01")).toBeNull();
    expect(rangeProblem("", "2026-01-31")).toBe("missing");
    expect(rangeProblem("2026-02-30", "2026-03-01")).toBe("missing");
    expect(rangeProblem("2026-02-01", "2026-01-31")).toBe("from-after-to");
    // 3660 days, both ends included, is the longest range; one more day is too long.
    expect(rangeProblem("2016-01-01", "2026-01-07")).toBeNull();
    expect(rangeProblem("2016-01-01", "2026-01-08")).toBe("too-long");
    expect(rangeProblemText("too-long")).toMatch(/ten years/);
    expect(rangeProblemText("from-after-to")).toMatch(/after the end date/);
    expect(rangeProblemText("missing")).toMatch(/start date and an end date/);
  });

  it("resolves a custom period only when it is valid", () => {
    expect(resolvePeriod("custom", { from: "2026-03-05", to: "2026-04-10" }, "2026-11-10")).toEqual({ range: { from: "2026-03-05", to: "2026-04-10" }, problem: null });
    expect(resolvePeriod("custom", { from: "2026-04-11", to: "2026-04-10" }, "2026-11-10")).toEqual({ range: null, problem: "from-after-to" });
    // A preset ignores whatever custom dates are left over.
    expect(resolvePeriod("last-month", { from: "", to: "" }, "2026-11-10")).toEqual({ range: { from: "2026-10-01", to: "2026-10-31" }, problem: null });
  });

  it("works in months", () => {
    expect(monthOf("2026-11-30")).toBe("2026-11");
    expect(shiftMonth("2026-11", -1)).toBe("2026-10");
    expect(shiftMonth("2026-11", -12)).toBe("2025-11");
    expect(shiftMonth("2026-01", -1)).toBe("2025-12");
    expect(shiftMonth("2026-12", 1)).toBe("2027-01");
  });

  it("describes a range in words", () => {
    expect(describeRange({ from: "2026-11-01", to: "2026-11-30" }, "en-US")).toBe("Nov 1, 2026 to Nov 30, 2026");
  });
});
