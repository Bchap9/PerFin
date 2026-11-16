// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link, useRouter, type ErrorComponentProps } from "@tanstack/react-router";

import { Spinner } from "../components/Spinner";
import { ErrorFallback } from "./ErrorFallback";

export function NotFoundPage() {
  return (
    <section className="status-page" aria-labelledby="not-found-title">
      <h1 id="not-found-title">Page not found</h1>
      <p>There is no page at this address.</p>
      <Link to="/" className="btn btn--primary btn--md">
        Go to Home
      </Link>
    </section>
  );
}

export function RouteErrorPage({ error, reset }: ErrorComponentProps) {
  const router = useRouter();
  return (
    <ErrorFallback
      error={error}
      onRetry={() => {
        reset();
        void router.invalidate();
      }}
    />
  );
}

export function PendingPage() {
  return (
    <div className="status-page">
      <Spinner size="lg" label="Loading page" />
    </div>
  );
}
