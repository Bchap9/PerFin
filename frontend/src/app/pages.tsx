// SPDX-License-Identifier: AGPL-3.0-or-later
import { Link, useRouter, type ErrorComponentProps } from "@tanstack/react-router";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { InboxIcon } from "../components/icons";
import { Spinner } from "../components/Spinner";
import { ErrorFallback } from "./ErrorFallback";

export function PlaceholderPage({ title, summary }: { title: string; summary: string }) {
  return (
    <section aria-labelledby="page-title">
      <div className="page-header">
        <h1 id="page-title">{title}</h1>
      </div>
      <Card>
        <EmptyState icon={<InboxIcon />} title="Not built yet" description={`${summary} This section arrives in a later release.`} />
      </Card>
    </section>
  );
}

export function NotFoundPage() {
  return (
    <section className="status-page" aria-labelledby="not-found-title">
      <h1 id="not-found-title">Page not found</h1>
      <p>There is no page at this address.</p>
      <Link to="/" className="btn btn--primary btn--md">
        Go to dashboard
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

export function LoginPage() {
  return (
    <section aria-labelledby="login-title">
      <h1 id="login-title">Sign in</h1>
      <Card>
        <p>Sign-in is not available yet. Local accounts with multi-factor authentication and single sign-on arrive in Phase 1.</p>
        <Link to="/" className="link-action link-action--back">Back to the dashboard</Link>
      </Card>
    </section>
  );
}
