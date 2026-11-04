// SPDX-License-Identifier: AGPL-3.0-or-later
import "./planning.css";

import type { ReactNode } from "react";

import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";

/** Page chrome shared by the planning screens: a heading with actions, then content. */
export function PlanningPage({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby="page-title" className="planning-stack">
      <div className="page-header">
        <h1 id="page-title">{title}</h1>
        {actions === undefined ? null : <div className="planning-actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/** Loading and error states for a query; renders `children` once data exists. */
export function QueryState({
  loading,
  error,
  label,
  children,
}: {
  loading: boolean;
  error: unknown;
  label: string;
  children: ReactNode;
}) {
  if (loading) {
    return <Spinner label={`Loading ${label}`} />;
  }
  if (error !== null && error !== undefined) {
    return (
      <p role="alert" className="auth-error">
        Could not load {label}. {errorMessage(error)}
      </p>
    );
  }
  return <>{children}</>;
}

export function ErrorLine({ message }: { message: string | null }) {
  return message === null ? null : (
    <p role="alert" className="auth-error">
      {message}
    </p>
  );
}
