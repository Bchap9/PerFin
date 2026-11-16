// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The top of a page: optional breadcrumbs, the title, a one-line description
 * and actions. The title is the page's `h1`, or an `h2` inside a section
 * layout, which renders the section's `h1` itself and says so through
 * `SectionHeadingContext`. A page therefore has exactly one `h1` wherever it
 * is mounted.
 */
import { createContext, useContext, type ReactNode } from "react";

import { Breadcrumbs, type Crumb } from "./Breadcrumbs";

/**
 * The id of the page's one `h1`. `main` is labelled by it (RootLayout), so a
 * screen reader names the content after the page; every place that renders a
 * page's `h1` gives it this id, and only that `h1` has it.
 */
export const PAGE_TITLE_ID = "page-title";

/** True inside a layout that already renders the page's `h1` (SectionLayout, SettingsLayout). */
export const SectionHeadingContext = createContext(false);

export interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  breadcrumbs?: readonly Crumb[];
}

export function PageHeader({ title, description, actions, breadcrumbs }: PageHeaderProps) {
  const inSection = useContext(SectionHeadingContext);
  const Heading = inSection ? "h2" : "h1";
  return (
    <div className="page-header page-header--stacked">
      {breadcrumbs === undefined || breadcrumbs.length === 0 ? null : <Breadcrumbs items={breadcrumbs} />}
      <div className="page-header__row">
        <div className="page-header__text">
          <Heading id={inSection ? undefined : PAGE_TITLE_ID} className="page-header__title">
            {title}
          </Heading>
          {description === undefined ? null : <p className="page-header__description">{description}</p>}
        </div>
        {actions === undefined ? null : <div className="page-header__actions">{actions}</div>}
      </div>
    </div>
  );
}
