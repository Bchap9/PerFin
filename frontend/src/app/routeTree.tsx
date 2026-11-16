// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The one route tree (plan, Decision 1). Each section's pages are children of
 * a layout route: `/inbox`, `/accounts`, `/plan` and `/reports` use
 * `SectionLayout`, `/settings` uses `SettingsLayout`. A child with
 * `staticData: { detail: true }` is a detail page that renders its own `h1`.
 *
 * Every page loads on demand (`lazyRouteComponent`) except Home and
 * Transactions, which are part of the first load (spec section 10). The
 * paths match `NEW_ROUTES` in routeMap.ts; the old addresses are added by
 * `redirectRoutes` (redirects.tsx). Static paths win over parameters
 * (`/accounts/investments` before `/accounts/$accountId`).
 *
 * To add a page: add its path to routeMap.ts (`NEW_ROUTES`, and a tab when
 * it has one), then one `page(...)` line here under its section.
 */
import { createRoute, lazyRouteComponent, redirect, type AnyRoute, type RouteComponent } from "@tanstack/react-router";

import { routes as authRoutes } from "../features/auth/routes";
import { HomePage } from "../features/dashboard/HomePage";
import { routes as setupRoutes } from "../features/setup/routes";
import { TransactionsPage } from "../features/transactions/TransactionsPage";
import { redirectRoutes } from "./redirects";
import type { SectionId } from "./routeMap";
import { SectionLayout } from "./SectionLayout";

interface PageOptions {
  detail?: true;
}

function page(parent: AnyRoute, path: string, component: RouteComponent, options: PageOptions = {}): AnyRoute {
  return createRoute({
    getParentRoute: () => parent,
    path,
    component,
    ...(options.detail === true ? { staticData: { detail: true } } : {}),
  });
}

function sectionLayout(root: AnyRoute, path: string, section: SectionId): AnyRoute {
  return createRoute({
    getParentRoute: () => root,
    path,
    component: function Section() {
      return <SectionLayout section={section} />;
    },
  });
}

const lazy = {
  inbox: lazyRouteComponent(() => import("../features/inbox/InboxPage"), "InboxPage"),
  inboxHistory: lazyRouteComponent(() => import("../features/inbox/InboxHistoryPage"), "InboxHistoryPage"),
  documents: lazyRouteComponent(() => import("../features/documents/DocumentsPage"), "DocumentsPage"),
  document: lazyRouteComponent(() => import("../features/documents/DocumentDetailPage"), "DocumentDetailPage"),
  mapping: lazyRouteComponent(() => import("../features/imports/MappingEditor"), "MappingEditorPage"),
  accounts: lazyRouteComponent(() => import("../features/accounts/AccountsHomePage"), "AccountsHomePage"),
  accountHub: lazyRouteComponent(() => import("../features/accounts/AccountHubPage"), "AccountHubPage"),
  investments: lazyRouteComponent(() => import("../features/investments/InvestmentsPage"), "InvestmentsPage"),
  subscriptions: lazyRouteComponent(() => import("../features/subscriptions/SubscriptionsPage"), "SubscriptionsPage"),
  subscription: lazyRouteComponent(() => import("../features/subscriptions/SubscriptionDetail"), "SubscriptionDetail"),
  budgets: lazyRouteComponent(() => import("../features/budgets/BudgetsPage"), "BudgetsPage"),
  budget: lazyRouteComponent(() => import("../features/budgets/BudgetDetailPage"), "BudgetDetailPage"),
  goals: lazyRouteComponent(() => import("../features/goals/GoalsPage"), "GoalsPage"),
  upcoming: lazyRouteComponent(() => import("../features/calendar/CalendarPage"), "CalendarPage"),
  reports: lazyRouteComponent(() => import("../features/reports/ReportsPage"), "ReportsPage"),
  netWorth: lazyRouteComponent(() => import("../features/networth/NetWorthPage"), "NetWorthPage"),
  taxYears: lazyRouteComponent(() => import("../features/tax/TaxYearsPage"), "TaxYearsPage"),
  taxYear: lazyRouteComponent(() => import("../features/tax/TaxYearPage"), "TaxYearPage"),
  settingsLayout: lazyRouteComponent(() => import("../features/settings/SettingsLayout"), "SettingsLayout"),
  settingsIndex: lazyRouteComponent(() => import("../features/settings/SettingsLayout"), "SettingsIndexPage"),
  profile: lazyRouteComponent(() => import("../features/settings/ProfilePage"), "ProfilePage"),
  security: lazyRouteComponent(() => import("../features/settings/SecurityPage"), "SecurityPage"),
  members: lazyRouteComponent(() => import("../features/settings/HouseholdMembersPage"), "HouseholdMembersPage"),
  general: lazyRouteComponent(() => import("../features/settings/GeneralPage"), "GeneralPage"),
  exchangeRates: lazyRouteComponent(() => import("../features/settings/ExchangeRatesPage"), "ExchangeRatesPage"),
  access: lazyRouteComponent(() => import("../features/settings/AccessPage"), "AccessPage"),
  categories: lazyRouteComponent(() => import("../features/categories/CategoriesSettingsPage"), "CategoriesSettingsPage"),
  intake: lazyRouteComponent(() => import("../features/settings/IntakePage"), "IntakePage"),
  ai: lazyRouteComponent(() => import("../features/settings/ai/AiAccessPage"), "AiAccessPage"),
  data: lazyRouteComponent(() => import("../features/settings/DataPage"), "DataPage"),
  notifications: lazyRouteComponent(() => import("../features/settings/notifications/NotificationsPage"), "NotificationsPage"),
  users: lazyRouteComponent(() => import("../features/settings/UsersPage"), "UsersPage"),
  rules: lazyRouteComponent(() => import("../features/rules/RulesPage"), "RulesPage"),
  newRule: lazyRouteComponent(() => import("../features/rules/RuleEditorPage"), "NewRulePage"),
  editRule: lazyRouteComponent(() => import("../features/rules/RuleEditorPage"), "EditRulePage"),
  learned: lazyRouteComponent(() => import("../features/imports/LearnedPage"), "LearnedPage"),
};

function inboxRoutes(root: AnyRoute): AnyRoute {
  const layout = sectionLayout(root, "/inbox", "inbox");
  return layout.addChildren([
    page(layout, "/", lazy.inbox),
    page(layout, "documents", lazy.documents),
    page(layout, "documents/$documentId", lazy.document, { detail: true }),
    page(layout, "documents/$documentId/mapping", lazy.mapping, { detail: true }),
    page(layout, "history", lazy.inboxHistory),
  ]);
}

function accountsRoutes(root: AnyRoute): AnyRoute {
  const layout = sectionLayout(root, "/accounts", "accounts");
  return layout.addChildren([
    page(layout, "/", lazy.accounts),
    page(layout, "investments", lazy.investments),
    page(layout, "$accountId", lazy.accountHub, { detail: true }),
  ]);
}

function planRoutes(root: AnyRoute): AnyRoute {
  const layout = sectionLayout(root, "/plan", "plan");
  const index = createRoute({
    getParentRoute: () => layout,
    path: "/",
    beforeLoad: () => {
      // The thrown redirect is the router's own control flow, not an error.
      // eslint-disable-next-line @typescript-eslint/only-throw-error
      throw redirect({ to: "/plan/subscriptions", replace: true });
    },
  }) as unknown as AnyRoute;
  // A series opens as a child of the list, so the list stays behind its dialog (stage 2).
  const subscriptions = page(layout, "subscriptions", lazy.subscriptions);
  return layout.addChildren([
    index,
    subscriptions.addChildren([page(subscriptions, "$id", lazy.subscription)]),
    page(layout, "budgets", lazy.budgets),
    page(layout, "budgets/$budgetId", lazy.budget, { detail: true }),
    page(layout, "goals", lazy.goals),
    page(layout, "upcoming", lazy.upcoming),
  ]);
}

function reportsRoutes(root: AnyRoute): AnyRoute {
  const layout = sectionLayout(root, "/reports", "reports");
  return layout.addChildren([
    page(layout, "/", lazy.reports),
    page(layout, "net-worth", lazy.netWorth),
    page(layout, "tax", lazy.taxYears),
    page(layout, "tax/$year", lazy.taxYear, { detail: true }),
  ]);
}

function settingsRoutes(root: AnyRoute): AnyRoute {
  const layout = page(root, "/settings", lazy.settingsLayout);
  return layout.addChildren([
    page(layout, "/", lazy.settingsIndex),
    page(layout, "profile", lazy.profile),
    page(layout, "security", lazy.security),
    page(layout, "household", lazy.members),
    page(layout, "general", lazy.general),
    page(layout, "exchange-rates", lazy.exchangeRates),
    page(layout, "access", lazy.access),
    page(layout, "categories", lazy.categories),
    page(layout, "intake", lazy.intake),
    page(layout, "ai", lazy.ai),
    page(layout, "data", lazy.data),
    page(layout, "notifications", lazy.notifications),
    page(layout, "users", lazy.users),
    page(layout, "rules", lazy.rules),
    page(layout, "rules/new", lazy.newRule),
    page(layout, "rules/$ruleId", lazy.editRule),
    page(layout, "learned", lazy.learned),
  ]);
}

/** Every route under `root`: the sections, Settings, the bare pages and the redirects. */
export function buildRouteTree(root: AnyRoute): AnyRoute[] {
  return [
    page(root, "/", HomePage),
    inboxRoutes(root),
    page(root, "/transactions", TransactionsPage),
    accountsRoutes(root),
    planRoutes(root),
    reportsRoutes(root),
    settingsRoutes(root),
    ...authRoutes(root),
    ...setupRoutes(root),
    ...redirectRoutes(root),
  ];
}
