// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Home (`/`, design note, stage 3, section 3): one fixed page, nothing on it
 * hidden or moved. A greeting, then net worth and what needs the person,
 * spending this month and what is coming up, and the latest transactions.
 * A household with no accounts sees "Start here" instead of the panels.
 */
import "./dashboard.css";

import { Link } from "@tanstack/react-router";

import { PageHeader } from "../../components/PageHeader";
import { Spinner } from "../../components/Spinner";
import { todayIsoDate } from "../../lib/dates";
import { useCanWrite } from "../accounts/api";
import { useAuthSession } from "../auth/session";
import { useAccounts } from "../planning/queries";
import { GettingStarted } from "./GettingStarted";
import { greeting } from "./home";
import { ComingUpPanel, LatestPanel, NeedsYouPanel, NetWorthPanel, SpendingPanel } from "./panels";

export function HomePage() {
  const { session } = useAuthSession();
  const accounts = useAccounts();
  const canWrite = useCanWrite();
  const now = new Date();
  const today = todayIsoDate(undefined, now);
  const name = session?.user.display_name.trim() ?? "";
  const title = name === "" ? greeting(now) : `${greeting(now)}, ${name}`;
  return (
    <div className="home">
      <PageHeader
        title={title}
        description="Your money at a glance."
        actions={
          // A viewer's Inbox has no uploader, so the action would lead nowhere.
          canWrite ? (
            <Link to="/inbox" hash="inbox-add-documents" className="btn btn--primary btn--md home-add">
              Add documents
            </Link>
          ) : undefined
        }
      />
      {accounts.isPending ? (
        <Spinner label="Loading your accounts" />
      ) : accounts.isSuccess && accounts.data.length === 0 ? (
        <GettingStarted />
      ) : (
        <>
          <div className="home-grid">
            <NetWorthPanel today={today} />
            <NeedsYouPanel />
          </div>
          <div className="home-grid">
            <SpendingPanel today={today} />
            <ComingUpPanel today={today} />
          </div>
          <LatestPanel />
        </>
      )}
    </div>
  );
}
