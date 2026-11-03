// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Primary navigation. Each section is a feature (src/features/<name>); until
 * its feature registers a route at `to`, the shell serves a placeholder page
 * there (src/app/shellRoutes.tsx).
 */
export interface NavItem {
  to: string;
  label: string;
  /** One line for the placeholder page. */
  summary: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  { to: "/", label: "Dashboard", summary: "Balances, cash flow and what needs attention." },
  { to: "/accounts", label: "Accounts", summary: "Bank, card, brokerage, loan and manual accounts." },
  { to: "/transactions", label: "Transactions", summary: "Search, categorise and split transactions." },
  { to: "/budgets", label: "Budgets", summary: "Monthly budgets by category." },
  { to: "/investments", label: "Investments", summary: "Holdings, lots and performance." },
  { to: "/loans", label: "Loans", summary: "Balances, amortisation and payoff scenarios." },
  { to: "/net-worth", label: "Net worth", summary: "Assets and liabilities over time." },
  { to: "/documents", label: "Documents", summary: "Statements and records, encrypted at rest." },
  { to: "/review", label: "Review", summary: "Items from imports that need a decision." },
  { to: "/tax", label: "Tax", summary: "Tax-year checklists and exports." },
  { to: "/settings", label: "Settings", summary: "Household, security, intake and integrations." },
];
