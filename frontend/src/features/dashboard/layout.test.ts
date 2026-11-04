// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  addWidget,
  availableWidgets,
  DEFAULT_LAYOUT,
  effectiveLayout,
  moveWidget,
  removeWidget,
  resizeWidget,
  WIDGET_CATALOGUE,
  widgetInfo,
  type DashboardLayout,
} from "./layout";

const LAYOUT: DashboardLayout = [
  { widget: "cash_flow", size: "large" },
  { widget: "goals", size: "small" },
  { widget: "loans", size: "medium" },
];

describe("dashboard layout", () => {
  it("catalogues every widget once, and the default layout only catalogued ones", () => {
    const ids = WIDGET_CATALOGUE.map((info) => info.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toHaveLength(12);
    expect(DEFAULT_LAYOUT.every((entry) => ids.includes(entry.widget))).toBe(true);
    expect(new Set(DEFAULT_LAYOUT.map((entry) => entry.widget)).size).toBe(DEFAULT_LAYOUT.length);
  });

  it("shows the default until a layout is saved, and an empty saved layout as empty", () => {
    expect(effectiveLayout(null)).toBe(DEFAULT_LAYOUT);
    expect(effectiveLayout(undefined)).toBe(DEFAULT_LAYOUT);
    expect(effectiveLayout([])).toEqual([]);
  });

  it("lists what can still be added", () => {
    const ids = availableWidgets(LAYOUT).map((info) => info.id);
    expect(ids).not.toContain("goals");
    expect(ids).toContain("net_worth");
    expect(availableWidgets([])).toHaveLength(12);
  });

  it("adds a widget at its default size, once", () => {
    const added = addWidget(LAYOUT, "recent_transactions");
    expect(added.at(-1)).toEqual({ widget: "recent_transactions", size: widgetInfo("recent_transactions").defaultSize });
    expect(addWidget(added, "recent_transactions")).toEqual(added);
    expect(LAYOUT).toHaveLength(3);
  });

  it("removes, moves and resizes without changing the input", () => {
    expect(removeWidget(LAYOUT, "goals").map((e) => e.widget)).toEqual(["cash_flow", "loans"]);
    expect(moveWidget(LAYOUT, "loans", -1).map((e) => e.widget)).toEqual(["cash_flow", "loans", "goals"]);
    expect(moveWidget(LAYOUT, "cash_flow", 1).map((e) => e.widget)).toEqual(["goals", "cash_flow", "loans"]);
    expect(moveWidget(LAYOUT, "cash_flow", -1)).toEqual(LAYOUT);
    expect(moveWidget(LAYOUT, "loans", 1)).toEqual(LAYOUT);
    expect(resizeWidget(LAYOUT, "goals", "large")[1]).toEqual({ widget: "goals", size: "large" });
    expect(LAYOUT[1]).toEqual({ widget: "goals", size: "small" });
  });
});
