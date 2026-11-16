// SPDX-License-Identifier: AGPL-3.0-or-later
import { render, screen } from "@testing-library/react";

import { expectNoAxeViolations } from "../../test/axe";
import { SeriesChart, toSeriesPoints } from "./SeriesChart";

const points = toSeriesPoints([
  { date: "2026-01-31", amounts: { assets: "1200.00", liabilities: "400.00" } },
  { date: "2026-02-28", amounts: { assets: "1300.50", liabilities: "380.25" } },
  { date: "2026-03-31", amounts: { assets: "1410.00", liabilities: "360.00" } },
]);

const series = [
  { key: "assets", label: "Assets" },
  // A colour given in code, as callers passed before the series classes: it must not reach the SVG.
  { key: "liabilities", label: "Liabilities", color: "var(--color-danger, #b42318)" },
];

function renderChart() {
  return render(<SeriesChart points={points} series={series} currency="USD" label="Assets and liabilities" />);
}

describe("SeriesChart", () => {
  it("draws each series with its class and no colour attribute", () => {
    const { container } = renderChart();
    const lines = Array.from(container.querySelectorAll(".recharts-line"));
    expect(lines).toHaveLength(2);
    for (const line of lines) {
      for (const element of [line, ...Array.from(line.querySelectorAll("*"))]) {
        expect(element.getAttribute("stroke") ?? "").not.toMatch(/#|var\(/);
        expect(element.getAttribute("fill") ?? "").not.toMatch(/#|var\(/);
      }
    }
    expect(lines[0]).toHaveClass("chart-series-1");
    expect(lines[1]).toHaveClass("chart-series-2");
    expect(container.querySelector(".recharts-cartesian-grid .chart-grid")).not.toBeNull();
    expect(container.querySelectorAll(".chart-axis").length).toBeGreaterThanOrEqual(2);
  });

  it("has a label naming the series", () => {
    renderChart();
    const chart = screen.getByRole("img", { name: "Assets and liabilities, 3 points" });
    expect(chart).toHaveAccessibleDescription(/Assets/);
    expect(chart).toHaveAccessibleDescription(/Liabilities/);
    const legend = screen.getByRole("list", { name: "Series" });
    const swatches = legend.querySelectorAll("svg rect, svg line");
    expect(swatches[0]).toHaveClass("chart-series-1");
    expect(swatches[1]).toHaveClass("chart-series-2");
  });

  it("has no axe violations", async () => {
    const { container } = renderChart();
    await expectNoAxeViolations(container);
  });
});
