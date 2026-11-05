// SPDX-License-Identifier: AGPL-3.0-or-later
import type { Valuation } from "./api";
import { valuationPoints, valueChange } from "./valuations";

const v = (id: string, valued_on: string, amount: string): Valuation => ({
  id,
  account_id: "a",
  valued_on,
  value: { amount, currency: "USD" },
  source: "manual",
  created_at: "2026-11-01T10:00:00Z",
});

describe("valuation history", () => {
  const rows = [v("3", "2026-06-01", "330000.00"), v("1", "2025-06-01", "300000.00"), v("2", "2026-01-01", "310000.00")];

  it("plots oldest first even though the API lists newest first", () => {
    expect(valuationPoints(rows).map((p) => p.date)).toEqual(["2025-06-01", "2026-01-01", "2026-06-01"]);
  });

  it("measures the change from the first to the latest value", () => {
    expect(valueChange(rows)).toEqual({ change: "30000.00", percent: "10.00", direction: "up" });
    expect(valueChange([v("1", "2025-01-01", "100"), v("2", "2026-01-01", "90")])).toMatchObject({ change: "-10", direction: "down" });
    expect(valueChange(rows.slice(0, 1))).toBeNull();
  });
});
