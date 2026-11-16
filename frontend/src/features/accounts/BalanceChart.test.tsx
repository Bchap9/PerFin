// SPDX-License-Identifier: AGPL-3.0-or-later
import { render } from "@testing-library/react";

import { BalanceChart } from "./BalanceChart";
import type { BalanceHistory } from "./api";

const history: BalanceHistory = {
  account_id: "00000000-0000-0000-0000-000000000001",
  currency: "USD",
  start: "2026-01-01",
  end: "2026-03-31",
  items: [
    { date: "2026-01-31", balance: { amount: "100.00", currency: "USD" } },
    { date: "2026-02-28", balance: { amount: "180.50", currency: "USD" } },
    { date: "2026-03-31", balance: { amount: "150.00", currency: "USD" } },
  ],
  snapshots: [],
} as BalanceHistory;

describe("BalanceChart", () => {
  it("draws the line in the series colour, not the text colour", () => {
    const { container } = render(<BalanceChart history={history} />);
    const line = container.querySelector(".recharts-line");
    expect(line).toHaveClass("chart-series-1");
    const curve = container.querySelector(".recharts-line-curve");
    expect(curve).not.toBeNull();
    // `currentColor` on the path beats the series class on its group, so the
    // line would take the body text colour (black in light, white in dark).
    expect(curve?.getAttribute("stroke")).not.toBe("currentColor");
  });
});
