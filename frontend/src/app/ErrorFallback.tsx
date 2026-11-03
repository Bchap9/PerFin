// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What the user sees when rendering fails. It shows a short message and
 * never a stack trace, internal identifiers or server internals (SECURITY.md
 * F5); `ApiError` messages come from problem-details titles/details only.
 */
import { Button } from "../components/Button";
import { errorMessage, isApiError } from "../lib/api/client";

export interface ErrorFallbackProps {
  error: unknown;
  onRetry?: () => void;
}

export function ErrorFallback({ error, onRetry }: ErrorFallbackProps) {
  const unauthenticated = isApiError(error) && error.isUnauthenticated;
  return (
    <section className="status-page" role="alert" aria-labelledby="error-title">
      <h1 id="error-title">{unauthenticated ? "Your session has ended" : "Something went wrong"}</h1>
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
