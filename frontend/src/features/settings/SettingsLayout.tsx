// SPDX-License-Identifier: AGPL-3.0-or-later
import "../auth/auth.css";
import "./settings.css";

import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { PAGE_TITLE_ID, PageHeader, SectionHeadingContext } from "../../components/PageHeader";
import { formatDateTime } from "../../lib/dates";
import { StepUpProvider } from "../auth/StepUp";
import { SettingsNav, useIsInstanceAdmin, visibleSettingsGroups } from "./SettingsNav";

/** Where rules and categories are made (spec section 1.3), said on the Settings index. */
export const SETTINGS_INTRO =
  "Rules and categories are made where you work, in Transactions and the Inbox. They are kept here to edit or remove.";

/** Said on top of the rules list and of "What PerFin learned" (plan, Task 19). */
export const RULES_INTRO =
  "Rules are made where you work: in a transaction's panel or from an Inbox card. Here you can change their order, edit or undo them.";

function useIsIndex(): boolean {
  return useRouterState({ select: (state) => state.location.pathname.replace(/\/+$/, "") === "/settings" });
}

/**
 * Settings: the one `h1`, the side list (from 1024 px; the index is the
 * list itself, so it is not repeated there), then the page, whose
 * `PageHeader` renders an `h2`. Every settings page can ask for step-up; the
 * dialog lives here.
 */
export function SettingsLayout() {
  const index = useIsIndex();
  return (
    <StepUpProvider>
      <header className="section-header settings-header">
        <h1 id={PAGE_TITLE_ID} className="section-header__title">
          Settings
        </h1>
      </header>
      <div className={index ? "settings-shell settings-shell--index" : "settings-shell"}>
        {index ? null : <SettingsNav />}
        <div className="settings-shell__page">
          <SectionHeadingContext.Provider value={true}>
            <Outlet />
          </SectionHeadingContext.Provider>
        </div>
      </div>
    </StepUpProvider>
  );
}

/** A settings page: breadcrumbs back to Settings, its title, and content. */
export function SettingsPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="settings-stack">
      <PageHeader title={title} breadcrumbs={[{ to: "/settings", label: "Settings" }, { label: title }]} />
      {children}
    </div>
  );
}

/** What each page holds, said under its link on the index. */
const DESCRIPTIONS: ReadonlyMap<string, string> = new Map([
  ["/settings/profile", "Your name, language, theme and time zone."],
  ["/settings/security", "Password, second factors, recovery codes, sessions and personal access tokens."],
  [
    "/settings/notifications",
    "Email, webhook, ntfy, Gotify or Matrix alerts for sign-in security, roles, reviews, tax and backups.",
  ],
  ["/settings/general", "The household's name and base currency."],
  ["/settings/household", "Who can see this household's finances, and their roles."],
  ["/settings/access", "Which accounts each member or viewer can see or edit."],
  ["/settings/exchange-rates", "The rates net worth and reports use to convert accounts in other currencies."],
  ["/settings/categories", "Categories, payees, tags and the tax hints each category carries."],
  ["/settings/rules", "The rules you made, in the order they run: edit, reorder, export or remove them."],
  ["/settings/learned", "Statement layouts, wording, column mappings and categories PerFin learned from your choices."],
  ["/settings/intake", "Receipts mailbox, its alias, the drop folder and virus scanning."],
  ["/settings/data", "Take a copy of everything, or restore one."],
  ["/settings/ai", "Assistants that may read financial facts, what leaves, and an audit of every call."],
  [
    "/settings/users",
    "Everyone with a sign-in on this PerFin instance: create accounts, disable them, reset second factors.",
  ],
]);

/** The Settings index: the intro, then one panel per group with its pages. On a phone it is the list. */
export function SettingsIndexPage() {
  const groups = visibleSettingsGroups(useIsInstanceAdmin());
  return (
    <div className="settings-stack">
      <p className="settings-intro">{SETTINGS_INTRO}</p>
      <ul className="settings-groups">
        {groups.map((group) => {
          const headingId = `settings-group-${group.label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
          return (
            <li key={group.label}>
              <section className="card settings-group" aria-labelledby={headingId}>
                <h2 id={headingId} className="card__title settings-group__title">
                  {group.label}
                </h2>
                <ul className="settings-group__pages">
                  {group.pages.map((page) => (
                    <li key={page.to} className="settings-group__page">
                      <Link to={page.to} className="settings-group__link">
                        {page.label}
                      </Link>
                      <p className="settings-group__text">{DESCRIPTIONS.get(page.to) ?? ""}</p>
                    </li>
                  ))}
                </ul>
              </section>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** A timestamp for tables; the raw value if it cannot be formatted. */
export function when(timestamp: string | null | undefined, empty = "Never"): string {
  if (timestamp === null || timestamp === undefined) {
    return empty;
  }
  return formatDateTime(timestamp) ?? timestamp;
}
