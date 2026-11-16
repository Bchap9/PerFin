// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The layout of a section (Inbox, Accounts, Plan, Reports): the section's
 * `h1`, its one-line description and its tab bar, then the page, whose
 * `PageHeader` renders an `h2` (SectionHeadingContext). A detail page (a
 * route with `staticData: { detail: true }`) gets neither and renders its own
 * `h1` and breadcrumbs.
 */
import { Outlet, useMatches } from "@tanstack/react-router";

import { PAGE_TITLE_ID, SectionHeadingContext } from "../components/PageHeader";
import { SectionNav, type SectionNavItem } from "../components/SectionNav";
import { useInboxSummary } from "./inboxSummary";
import { SECTIONS, type Section, type SectionTab } from "./routeMap";

declare module "@tanstack/react-router" {
  interface StaticDataRouteOption {
    /** A detail page: the section layout renders only the page. */
    detail?: boolean;
  }
}

function useIsDetail(): boolean {
  return useMatches({ select: (matches) => matches.at(-1)?.staticData.detail === true });
}

function navItem(tab: SectionTab, inboxCount: number | undefined): SectionNavItem {
  return {
    to: tab.to,
    label: tab.label,
    ...(tab.search === undefined ? {} : { search: tab.search }),
    ...(tab.keepSearch === undefined ? {} : { keepSearch: tab.keepSearch }),
    // As on the rail badge: no count at zero, while loading or on an error.
    ...(tab.inboxCount === true && inboxCount !== undefined && inboxCount > 0 ? { count: inboxCount } : {}),
  };
}

function SectionHeader({ section }: { section: Section }) {
  const hasCount = section.tabs.some((tab) => tab.inboxCount === true);
  return hasCount ? <HeaderWithCount section={section} /> : <HeaderBody section={section} />;
}

function HeaderWithCount({ section }: { section: Section }) {
  const summary = useInboxSummary();
  return <HeaderBody section={section} inboxCount={summary.isError ? undefined : summary.data?.cards} />;
}

function HeaderBody({ section, inboxCount }: { section: Section; inboxCount?: number | undefined }) {
  return (
    <header className="section-header">
      <h1 id={PAGE_TITLE_ID} className="section-header__title">
        {section.label}
      </h1>
      <p className="section-header__description">{section.description}</p>
      {section.tabs.length === 0 ? null : <SectionNav label={section.label} items={section.tabs.map((tab) => navItem(tab, inboxCount))} />}
    </header>
  );
}

export function SectionLayout({ section }: { section: Section["id"] }) {
  const detail = useIsDetail();
  const entry = SECTIONS.find((candidate) => candidate.id === section);
  if (detail || entry === undefined) {
    return <Outlet />;
  }
  return (
    <>
      <SectionHeader section={entry} />
      <SectionHeadingContext.Provider value={true}>
        <Outlet />
      </SectionHeadingContext.Provider>
    </>
  );
}
