// SPDX-License-Identifier: AGPL-3.0-or-later
import "../auth/auth.css";

import { Link, Outlet } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { Card } from "../../components/Card";
import { formatDateTime } from "../../lib/dates";
import { StepUpProvider } from "../auth/StepUp";

/** Every settings page can ask for step-up; the dialog lives here. */
export function SettingsLayout() {
  return (
    <StepUpProvider>
      <Outlet />
    </StepUpProvider>
  );
}

/** A settings page: heading, a link back to the settings overview and content. */
export function SettingsPage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-labelledby="page-title" className="settings-stack">
      <div className="page-header">
        <h1 id="page-title">{title}</h1>
      </div>
      <p>
        <Link to="/settings">All settings</Link>
      </p>
      {children}
    </section>
  );
}

const SECTIONS = [
  { to: "/settings/profile", title: "Profile", text: "Your name, language, theme and time zone." },
  {
    to: "/settings/security",
    title: "Security",
    text: "Password, second factors, recovery codes, sessions and personal access tokens.",
  },
  { to: "/settings/household", title: "Household members", text: "Who can see this household's finances, and their roles." },
] as const;

export function SettingsIndexPage() {
  return (
    <section aria-labelledby="page-title" className="settings-stack">
      <div className="page-header">
        <h1 id="page-title">Settings</h1>
      </div>
      <ul className="settings-links">
        {SECTIONS.map((section) => (
          <li key={section.to}>
            <Card>
              <h2 className="card__title">
                <Link to={section.to}>{section.title}</Link>
              </h2>
              <p className="auth-muted">{section.text}</p>
            </Card>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** A timestamp for tables; the raw value if it cannot be formatted. */
export function when(timestamp: string | null | undefined, empty = "Never"): string {
  if (timestamp === null || timestamp === undefined) {
    return empty;
  }
  return formatDateTime(timestamp) ?? timestamp;
}
