// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the Reports page shows, kept in the address (`/reports?tab=...`) so a
 * view can be bookmarked and the Back button returns to it after opening a
 * row's transactions. Anything unknown or out of range falls back to the
 * default; defaults are left out of the address.
 */
import { isIsoDate } from "../../lib/dates";
import type { Flow, GroupBy } from "./api";
import { isPeriodId, type PeriodId } from "./periods";

export const TAB_IDS = ["spending", "income", "cashflow", "trends"] as const;
export type TabId = (typeof TAB_IDS)[number];

export const GROUP_OPTIONS: readonly { value: GroupBy; label: string }[] = [
  { value: "category", label: "Category" },
  { value: "payee", label: "Payee" },
  { value: "month", label: "Month" },
  { value: "account", label: "Account" },
];

/** Category levels the server can roll up to (DATA_MODEL: at most three). */
export const DEPTH_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 1, label: "Main categories" },
  { value: 2, label: "Subcategories" },
  { value: 3, label: "Most detailed" },
];

/** Lengths of the monthly series on the Trends tab (the server allows 1 to 60). */
export const TREND_MONTH_OPTIONS: readonly number[] = [6, 13, 25, 37, 60];
/** Thirteen months include the same month a year earlier. */
export const DEFAULT_TREND_MONTHS = 13;

export interface ReportState {
  tab: TabId;
  period: PeriodId;
  /** Custom range; only used when `period` is "custom". */
  from: string;
  to: string;
  /** Account id, or "" for all accounts. */
  account: string;
  /** `null`: follow the household setting. */
  pending: boolean | null;
  /** Grouping of the Spending and Income tabs. */
  group: GroupBy;
  depth: number;
  /** Which flow the Trends tab compares. */
  flow: Flow;
  months: number;
}

export const DEFAULT_STATE: ReportState = {
  tab: "spending",
  period: "this-month",
  from: "",
  to: "",
  account: "",
  pending: null,
  group: "category",
  depth: 1,
  flow: "spending",
  months: DEFAULT_TREND_MONTHS,
};

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.find((entry) => entry === value) ?? fallback;
}

function numberOf(value: unknown, allowed: readonly number[], fallback: number): number {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return allowed.includes(parsed) ? parsed : fallback;
}

function booleanOf(value: unknown): boolean | null {
  if (value === true || value === "true") {
    return true;
  }
  return value === false || value === "false" ? false : null;
}

const MAX_ID_LENGTH = 64;

function idOf(value: unknown): string {
  const text = typeof value === "string" ? value : typeof value === "number" ? String(value) : "";
  return text.length <= MAX_ID_LENGTH ? text : "";
}

const dateOf = (value: unknown): string => (isIsoDate(value) ? value : "");

/** Read the page state from the router's search object (values may be strings, numbers or booleans). */
export function parseReportSearch(search: Readonly<Record<string, unknown>>): ReportState {
  const get = (key: string): unknown => new Map(Object.entries(search)).get(key);
  const period = get("period");
  return {
    tab: oneOf(get("tab"), TAB_IDS, DEFAULT_STATE.tab),
    period: isPeriodId(period) ? period : DEFAULT_STATE.period,
    from: dateOf(get("from")),
    to: dateOf(get("to")),
    account: idOf(get("account")),
    pending: booleanOf(get("pending")),
    group: oneOf(
      get("group"),
      GROUP_OPTIONS.map((option) => option.value),
      DEFAULT_STATE.group,
    ),
    depth: numberOf(
      get("depth"),
      DEPTH_OPTIONS.map((option) => option.value),
      DEFAULT_STATE.depth,
    ),
    flow: oneOf(get("flow"), ["spending", "income"] as const, DEFAULT_STATE.flow),
    months: numberOf(get("months"), TREND_MONTH_OPTIONS, DEFAULT_STATE.months),
  };
}

export type ReportSearch = Record<string, string | number | boolean>;

/** The search object for a state: defaults and unused custom dates are left out. */
export function reportSearch(state: ReportState): ReportSearch {
  const custom = state.period === "custom";
  return {
    ...(state.tab === DEFAULT_STATE.tab ? {} : { tab: state.tab }),
    ...(state.period === DEFAULT_STATE.period ? {} : { period: state.period }),
    ...(custom && state.from !== "" ? { from: state.from } : {}),
    ...(custom && state.to !== "" ? { to: state.to } : {}),
    ...(state.account === "" ? {} : { account: state.account }),
    ...(state.pending === null ? {} : { pending: state.pending }),
    ...(state.group === DEFAULT_STATE.group ? {} : { group: state.group }),
    ...(state.depth === DEFAULT_STATE.depth ? {} : { depth: state.depth }),
    ...(state.flow === DEFAULT_STATE.flow ? {} : { flow: state.flow }),
    ...(state.months === DEFAULT_STATE.months ? {} : { months: state.months }),
  };
}
