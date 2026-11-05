// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The dashboard's widget catalogue and the pure functions that edit a
 * layout. A layout is an ordered list of widgets, each at one of three
 * sizes; it is saved per person by `PATCH /settings/user`
 * (`dashboard_layout`), and `null` there means "never customised", which
 * shows the default layout.
 */
import type { UserPreferences } from "../settings/api";

export type DashboardLayout = NonNullable<UserPreferences["dashboard_layout"]>;
export type LayoutEntry = DashboardLayout[number];
export type WidgetId = LayoutEntry["widget"];
export type WidgetSize = LayoutEntry["size"];

export interface WidgetInfo {
  id: WidgetId;
  title: string;
  description: string;
  defaultSize: WidgetSize;
}

export const WIDGET_CATALOGUE: readonly WidgetInfo[] = [
  { id: "net_worth", title: "Net worth", description: "Assets minus liabilities today.", defaultSize: "medium" },
  { id: "cash_flow", title: "Cash flow", description: "This month's income, spending and net.", defaultSize: "medium" },
  { id: "spending", title: "Spending by category", description: "Where this month's money went.", defaultSize: "medium" },
  { id: "budgets", title: "Budgets", description: "Progress of your budgets and what is over.", defaultSize: "medium" },
  { id: "goals", title: "Goals", description: "Savings goals with progress.", defaultSize: "medium" },
  { id: "recent_transactions", title: "Recent transactions", description: "The latest transactions.", defaultSize: "large" },
  { id: "review_queue", title: "Needs review", description: "Imported items waiting for a decision.", defaultSize: "small" },
  { id: "upcoming_bills", title: "Upcoming bills", description: "Bills due in the next 30 days.", defaultSize: "medium" },
  { id: "holdings", title: "Holdings and allocation", description: "Portfolio value and asset allocation.", defaultSize: "medium" },
  { id: "loans", title: "Loans", description: "Balances and next payments.", defaultSize: "medium" },
  { id: "tax_documents", title: "Tax documents", description: "Tax documents still missing.", defaultSize: "small" },
  { id: "accounts", title: "Accounts", description: "Balances of your open accounts.", defaultSize: "medium" },
];


export const SIZE_OPTIONS: readonly { value: WidgetSize; label: string }[] = [
  { value: "small", label: "Small" },
  { value: "medium", label: "Medium" },
  { value: "large", label: "Large" },
];

export const DEFAULT_LAYOUT: DashboardLayout = [
  { widget: "net_worth", size: "medium" },
  { widget: "cash_flow", size: "medium" },
  { widget: "review_queue", size: "small" },
  { widget: "tax_documents", size: "small" },
  { widget: "spending", size: "medium" },
  { widget: "budgets", size: "medium" },
  { widget: "accounts", size: "medium" },
  { widget: "upcoming_bills", size: "medium" },
  { widget: "goals", size: "medium" },
  { widget: "recent_transactions", size: "large" },
];

const BY_ID = new Map(WIDGET_CATALOGUE.map((info) => [info.id, info]));

export function widgetInfo(id: WidgetId): WidgetInfo {
  const info = BY_ID.get(id);
  if (info === undefined) {
    throw new Error(`unknown dashboard widget: ${id}`);
  }
  return info;
}

/** The layout to show: the saved one, or the default when none was saved. */
export function effectiveLayout(saved: DashboardLayout | null | undefined): DashboardLayout {
  return saved ?? DEFAULT_LAYOUT;
}

/** Widgets in the catalogue that are not on the page (the "add" list). */
export function availableWidgets(layout: readonly LayoutEntry[]): WidgetInfo[] {
  const used = new Set(layout.map((entry) => entry.widget));
  return WIDGET_CATALOGUE.filter((info) => !used.has(info.id));
}

export function addWidget(layout: readonly LayoutEntry[], id: WidgetId): DashboardLayout {
  if (layout.some((entry) => entry.widget === id)) {
    return [...layout];
  }
  return [...layout, { widget: id, size: widgetInfo(id).defaultSize }];
}

export function removeWidget(layout: readonly LayoutEntry[], id: WidgetId): DashboardLayout {
  return layout.filter((entry) => entry.widget !== id);
}

/** Move a widget one place earlier (`-1`) or later (`1`); at either end nothing changes. */
export function moveWidget(layout: readonly LayoutEntry[], id: WidgetId, direction: -1 | 1): DashboardLayout {
  const from = layout.findIndex((entry) => entry.widget === id);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= layout.length) {
    return [...layout];
  }
  const next = [...layout];
  const [moved] = next.splice(from, 1);
  if (moved !== undefined) {
    next.splice(to, 0, moved);
  }
  return next;
}

export function resizeWidget(layout: readonly LayoutEntry[], id: WidgetId, size: WidgetSize): DashboardLayout {
  return layout.map((entry) => (entry.widget === id ? { ...entry, size } : entry));
}
