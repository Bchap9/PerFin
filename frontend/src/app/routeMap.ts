// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The one route map (docs/superpowers/specs/2026-11-16-ui-redesign-design.md,
 * section 1.2). The rail, the section tab bars, the command palette, the
 * redirects and the redirect test all read it, so they cannot drift apart.
 *
 * - `SECTIONS`: the six sections in rail order, each with its tab bar.
 * - `SETTINGS_GROUPS`: the Settings side list (spec section 1.3).
 * - `REDIRECTS`: every old address that moved, one row each.
 * - `OLD_ROUTES`: every path registered before stage 3, generated (below).
 * - `NEW_ROUTES`: every path the route tree registers that is not a redirect.
 */
import type { SVGProps } from "react";

import { BarsIcon, CardIcon, HomeIcon, InboxIcon, ListIcon, TargetIcon } from "../components/icons";

export type SectionId = "home" | "inbox" | "transactions" | "accounts" | "plan" | "reports";

export interface SectionTab {
  to: string;
  label: string;
  search?: Readonly<Record<string, string>>;
  /** Search parameters the link carries over from the current address. */
  keepSearch?: readonly string[];
  /** The tab shows the Inbox card count (`app/inboxSummary.ts`). */
  inboxCount?: true;
}

export interface Section {
  id: SectionId;
  label: string;
  to: string;
  icon: (props: Omit<SVGProps<SVGSVGElement>, "children">) => React.JSX.Element;
  description: string;
  tabs: readonly SectionTab[];
}

/** The Reports views under `?tab=` keep the period, account and pending choices (Decision 5). */
const REPORT_KEEP = ["period", "from", "to", "account", "pending"] as const;

export const SECTIONS: readonly Section[] = [
  { id: "home", label: "Home", to: "/", icon: HomeIcon, description: "Where your money stands and what changed.", tabs: [] },
  {
    id: "inbox",
    label: "Inbox",
    to: "/inbox",
    icon: InboxIcon,
    description: "Everything that waits on you: imports to check, suggestions and new subscriptions.",
    tabs: [
      { to: "/inbox", label: "Needs you", inboxCount: true },
      { to: "/inbox/documents", label: "Documents" },
      { to: "/inbox/history", label: "History" },
    ],
  },
  {
    id: "transactions",
    label: "Transactions",
    to: "/transactions",
    icon: ListIcon,
    description: "Every transaction of every account.",
    tabs: [],
  },
  {
    id: "accounts",
    label: "Accounts",
    to: "/accounts",
    icon: CardIcon,
    description: "Bank, card, investment, loan and property accounts.",
    tabs: [
      { to: "/accounts", label: "All accounts" },
      { to: "/accounts/investments", label: "Investments" },
    ],
  },
  {
    id: "plan",
    label: "Plan",
    to: "/plan/subscriptions",
    icon: TargetIcon,
    description: "Subscriptions, budgets, goals and what is coming up.",
    tabs: [
      { to: "/plan/subscriptions", label: "Subscriptions" },
      { to: "/plan/budgets", label: "Budgets" },
      { to: "/plan/goals", label: "Goals" },
      { to: "/plan/upcoming", label: "Upcoming" },
    ],
  },
  {
    id: "reports",
    label: "Reports",
    to: "/reports",
    icon: BarsIcon,
    description: "Spending, income, cash flow, trends, net worth and tax over any period.",
    tabs: [
      { to: "/reports", label: "Spending", keepSearch: REPORT_KEEP },
      { to: "/reports", label: "Income", search: { tab: "income" }, keepSearch: REPORT_KEEP },
      { to: "/reports", label: "Cash flow", search: { tab: "cashflow" }, keepSearch: REPORT_KEEP },
      { to: "/reports", label: "Trends", search: { tab: "trends" }, keepSearch: REPORT_KEEP },
      { to: "/reports/net-worth", label: "Net worth" },
      { to: "/reports/tax", label: "Tax" },
    ],
  },
];

/** The section an address belongs to, or `null` (Settings, sign-in, unknown). */
export function sectionForPath(pathname: string): SectionId | null {
  if (pathname === "/" || pathname === "") {
    return "home";
  }
  const first = pathname.split("/")[1] ?? "";
  const section = SECTIONS.find((entry) => entry.id !== "home" && entry.id === first);
  return section === undefined ? null : section.id;
}

export interface SettingsPageLink {
  to: string;
  label: string;
  /** Listed only for an instance administrator (the page itself checks again). */
  instanceAdmin?: true;
}

export const SETTINGS_GROUPS: readonly { label: string; pages: readonly SettingsPageLink[] }[] = [
  {
    label: "You",
    pages: [
      { to: "/settings/profile", label: "Profile" },
      { to: "/settings/security", label: "Security" },
      { to: "/settings/notifications", label: "Notifications" },
    ],
  },
  {
    label: "Household",
    pages: [
      { to: "/settings/general", label: "Name and currency" },
      { to: "/settings/household", label: "Members" },
      { to: "/settings/access", label: "Account access" },
      { to: "/settings/exchange-rates", label: "Exchange rates" },
    ],
  },
  {
    label: "Categorisation",
    pages: [
      { to: "/settings/categories", label: "Categories" },
      { to: "/settings/rules", label: "Rules" },
      { to: "/settings/learned", label: "What PerFin learned" },
    ],
  },
  { label: "Import", pages: [{ to: "/settings/intake", label: "Intake" }] },
  {
    label: "Data and integrations",
    pages: [
      { to: "/settings/data", label: "Export and import" },
      { to: "/settings/ai", label: "AI access" },
    ],
  },
  { label: "Instance", pages: [{ to: "/settings/users", label: "Users", instanceAdmin: true }] },
];

/**
 * The document title of an address (WCAG 2.4.2): its tab (`tab`, the `?tab=`
 * value, picks among tabs of one path), else its Settings page, else its
 * section, then "PerFin". The sign-in pages set their own (AuthLayout).
 */
export function pageTitle(pathname: string, tab: unknown): string {
  const tabs = SECTIONS.flatMap((section) => section.tabs).filter((entry) => entry.to === pathname);
  const page =
    (tabs.find((entry) => entry.search?.["tab"] === tab) ?? tabs.find((entry) => entry.search === undefined))?.label ??
    SETTINGS_GROUPS.flatMap((group) => group.pages).find((entry) => entry.to === pathname)?.label ??
    SECTIONS.find((section) => section.id === sectionForPath(pathname))?.label ??
    (pathname.startsWith("/settings") ? "Settings" : undefined);
  return page === undefined ? "PerFin" : `${page} · PerFin`;
}

export interface Redirect {
  from: string;
  to: string;
  /** Search values the new address adds; the old address's own search is kept. */
  search?: Readonly<Record<string, string>>;
  /** The new address needs the object's account, read from the API first. */
  data?: "loan" | "asset";
}

/** One row per "redirect" row of the spec's table (the `/plan` index redirect is part of the tree). */
export const REDIRECTS: readonly Redirect[] = [
  { from: "/review", to: "/inbox" },
  { from: "/documents", to: "/inbox" },
  { from: "/documents/library", to: "/inbox/documents" },
  { from: "/documents/$documentId", to: "/inbox/documents/$documentId" },
  { from: "/documents/$documentId/mapping", to: "/inbox/documents/$documentId/mapping" },
  { from: "/investments", to: "/accounts/investments" },
  { from: "/loans", to: "/accounts", search: { group: "loans" } },
  { from: "/loans/$loanId", to: "/accounts/$accountId", search: { tab: "schedule" }, data: "loan" },
  { from: "/assets", to: "/accounts", search: { group: "property" } },
  { from: "/assets/$assetId", to: "/accounts/$accountId", search: { tab: "valuations" }, data: "asset" },
  { from: "/subscriptions", to: "/plan/subscriptions" },
  { from: "/subscriptions/$id", to: "/plan/subscriptions/$id" },
  { from: "/budgets", to: "/plan/budgets" },
  { from: "/budgets/$budgetId", to: "/plan/budgets/$budgetId" },
  { from: "/goals", to: "/plan/goals" },
  { from: "/calendar", to: "/plan/upcoming" },
  { from: "/net-worth", to: "/reports/net-worth" },
  { from: "/tax", to: "/reports/tax" },
  { from: "/tax/$year", to: "/reports/tax/$year" },
  { from: "/tax/mapping", to: "/settings/categories", search: { tab: "tax" } },
  { from: "/rules", to: "/settings/rules" },
  { from: "/rules/new", to: "/settings/rules/new" },
  { from: "/rules/$ruleId", to: "/settings/rules/$ruleId" },
  { from: "/documents/learned", to: "/settings/learned" },
];

/*
 * Every path registered at the stage 2 head (a9f237b), generated with
 *   git grep -hoE 'path: "[^"]*"|page\((LOGIN|MFA|ENROLL|LINK_OIDC|FORGOT|RESET)_PATH|child\("[^"]*"' \
 *     a9f237b -- 'frontend/src/features/*\/routes.tsx'
 * plus the NAV_ITEMS paths of app/nav.ts, the auth constants of
 * features/auth/guard.ts and SETUP_PATH; settings children resolved against
 * /settings, the subscription child against /subscriptions; sorted, deduplicated.
 */
export const OLD_ROUTES: readonly string[] = [
  "/",
  "/accounts",
  "/accounts/$accountId",
  "/assets",
  "/assets/$assetId",
  "/budgets",
  "/budgets/$budgetId",
  "/calendar",
  "/documents",
  "/documents/$documentId",
  "/documents/$documentId/mapping",
  "/documents/learned",
  "/documents/library",
  "/forgot-password",
  "/goals",
  "/investments",
  "/loans",
  "/loans/$loanId",
  "/login",
  "/login/enroll-mfa",
  "/login/link-oidc",
  "/login/mfa",
  "/net-worth",
  "/reports",
  "/reset-password",
  "/review",
  "/rules",
  "/rules/$ruleId",
  "/rules/new",
  "/settings",
  "/settings/access",
  "/settings/ai",
  "/settings/categories",
  "/settings/data",
  "/settings/exchange-rates",
  "/settings/general",
  "/settings/household",
  "/settings/intake",
  "/settings/notifications",
  "/settings/profile",
  "/settings/security",
  "/settings/users",
  "/setup",
  "/subscriptions",
  "/subscriptions/$id",
  "/tax",
  "/tax/$year",
  "/tax/mapping",
  "/transactions",
];

/** The settings pages under `/settings/<path>`, in the order the tree registers them. */
export const SETTINGS_PATHS = [
  "profile",
  "security",
  "household",
  "general",
  "exchange-rates",
  "access",
  "categories",
  "intake",
  "ai",
  "data",
  "notifications",
  "users",
  "rules",
  "rules/new",
  "rules/$ruleId",
  "learned",
] as const;

export const NEW_ROUTES: readonly string[] = [
  "/",
  "/inbox",
  "/inbox/documents",
  "/inbox/documents/$documentId",
  "/inbox/documents/$documentId/mapping",
  "/inbox/history",
  "/transactions",
  "/accounts",
  "/accounts/investments",
  "/accounts/$accountId",
  "/plan/subscriptions",
  "/plan/subscriptions/$id",
  "/plan/budgets",
  "/plan/budgets/$budgetId",
  "/plan/goals",
  "/plan/upcoming",
  "/reports",
  "/reports/net-worth",
  "/reports/tax",
  "/reports/tax/$year",
  "/settings",
  ...SETTINGS_PATHS.map((path) => `/settings/${path}`),
  "/login",
  "/login/mfa",
  "/login/enroll-mfa",
  "/login/link-oidc",
  "/forgot-password",
  "/reset-password",
  "/setup",
];
