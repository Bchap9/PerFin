// SPDX-License-Identifier: AGPL-3.0-or-later
import { useId, type HTMLAttributes, type ReactNode } from "react";

import { cx } from "./cx";

export interface CardProps extends Omit<HTMLAttributes<HTMLElement>, "title"> {
  title?: ReactNode;
  /** Heading level for the title (default 2). */
  headingLevel?: 2 | 3 | 4;
  /** Controls shown at the right of the header. */
  actions?: ReactNode;
  footer?: ReactNode;
}

/** A titled surface. With a title it is a labelled `section` landmark. */
export function Card({ title, headingLevel = 2, actions, footer, className, children, ...rest }: CardProps) {
  const headingId = useId();
  const Heading = `h${String(headingLevel)}` as "h2" | "h3" | "h4";
  const hasTitle = title !== undefined && title !== null;
  return (
    <section className={cx("card", className)} aria-labelledby={hasTitle ? headingId : undefined} {...rest}>
      {hasTitle || actions !== undefined ? (
        <header className="card__header">
          {hasTitle ? (
            <Heading id={headingId} className="card__title">
              {title}
            </Heading>
          ) : null}
          {actions === undefined ? null : <div className="card__actions">{actions}</div>}
        </header>
      ) : null}
      <div className="card__body">{children}</div>
      {footer === undefined ? null : <footer className="card__footer">{footer}</footer>}
    </section>
  );
}
