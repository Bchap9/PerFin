// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReactNode } from "react";

import { cx } from "./cx";

export interface EmptyStateProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  action?: ReactNode;
  /** Heading level for the title (default 2). */
  headingLevel?: 2 | 3 | 4;
  className?: string;
}

export function EmptyState({ title, description, icon, action, headingLevel = 2, className }: EmptyStateProps) {
  const Heading = `h${String(headingLevel)}` as "h2" | "h3" | "h4";
  return (
    <div className={cx("empty-state", className)}>
      {icon === undefined ? null : (
        <div className="empty-state__icon" aria-hidden="true">
          {icon}
        </div>
      )}
      <Heading className="empty-state__title">{title}</Heading>
      {description === undefined ? null : <p className="empty-state__description">{description}</p>}
      {action === undefined ? null : <div className="empty-state__action">{action}</div>}
    </div>
  );
}
