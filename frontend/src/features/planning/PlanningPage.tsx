// SPDX-License-Identifier: AGPL-3.0-or-later
import "./planning.css";

import type { ReactNode } from "react";

import type { Crumb } from "../../components/Breadcrumbs";
import { useFocusInvalid } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";

/**
 * Page chrome shared by the planning screens: the page header (title,
 * optional description, breadcrumbs and actions), then content. The title is
 * the page's `h1`, or an `h2` inside a section layout (see `PageHeader`).
 */
export function PlanningPage({
  title,
  actions,
  children,
  description,
  breadcrumbs,
  titleHidden,
}: {
  title: string;
  /** See `PageHeader`: the title for screen readers only. */
  titleHidden?: boolean;
  actions?: ReactNode;
  children: ReactNode;
  description?: string;
  breadcrumbs?: readonly Crumb[];
}) {
  return (
    <section aria-label={title} className="planning-stack">
      <PageHeader
        title={title}
        {...(description === undefined ? {} : { description })}
        {...(breadcrumbs === undefined ? {} : { breadcrumbs })}
        {...(actions === undefined ? {} : { actions })}
        {...(titleHidden === undefined ? {} : { titleHidden })}
      />
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

/**
 * A planning dialog's fields as a form, so Enter in a field saves: the
 * dialog's footer button is `type="submit" form={id}`. `submit` checks the
 * fields itself (`noValidate`); the first field it marks invalid then takes focus.
 */
export function PlanForm({ id, submit, children }: { id: string; submit: () => Promise<void>; children: ReactNode }) {
  const focusInvalid = useFocusInvalid(id);
  return (
    <form
      id={id}
      className="planning-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        focusInvalid();
        void submit();
      }}
    >
      {children}
    </form>
  );
}
