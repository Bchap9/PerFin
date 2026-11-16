// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the user sees when rendering fails. It shows a short message and
 * never a stack trace, internal identifiers or server internals (SECURITY.md
 * F5); `ApiError` messages come from problem-details titles/details only.
 */
import { useContext } from "react";

import { Button } from "../components/Button";
import { PAGE_TITLE_ID, SectionHeadingContext } from "../components/PageHeader";
import { errorMessage, isApiError } from "../lib/api/client";

export interface ErrorFallbackProps {
  error: unknown;
  onRetry?: () => void;
}

/**
 * Inside a section or Settings the layout already shows the page's `h1`, so
 * the message is an `h2` there; elsewhere it is the page's `h1`.
 */
export function ErrorFallback({ error, onRetry }: ErrorFallbackProps) {
  const unauthenticated = isApiError(error) && error.isUnauthenticated;
  const inSection = useContext(SectionHeadingContext);
  const Heading = inSection ? "h2" : "h1";
  const headingId = inSection ? "error-title" : PAGE_TITLE_ID;
  return (
    <section className="status-page" role="alert" aria-labelledby={headingId}>
      <Heading id={headingId}>{unauthenticated ? "Your session has ended" : "Something went wrong"}</Heading>
      <p>{unauthenticated ? "Sign in again to continue." : errorMessage(error)}</p>
      <div className="page-header">
        {unauthenticated ? (
          <a className="btn btn--primary btn--md" href="/login">
            Sign in
          </a>
        ) : null}
        {onRetry === undefined ? null : (
          <Button variant={unauthenticated ? "secondary" : "primary"} onClick={onRetry}>
            Try again
          </Button>
        )}
        <a className="btn btn--secondary btn--md" href="/">
          Go to dashboard
        </a>
      </div>
    </section>
  );
}
