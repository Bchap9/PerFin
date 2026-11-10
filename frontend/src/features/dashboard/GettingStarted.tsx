// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The first screen of a household with no accounts: what to do first, in
 * order, each step a link. A dashboard of empty widgets says nothing about
 * where to begin. It disappears with the first account.
 */
import { Link } from "@tanstack/react-router";

import { Card } from "../../components/Card";
import { useCanWrite } from "../accounts/api";
import { useAccounts } from "../planning/queries";

export function GettingStarted() {
  const accounts = useAccounts();
  const canWrite = useCanWrite();
  if (!accounts.isSuccess || accounts.data.length > 0) {
    return null;
  }
  if (!canWrite) {
    return (
      <Card title="Nothing here yet">
        <p>This household has no accounts yet. Someone who can make changes needs to add one or upload a statement.</p>
      </Card>
    );
  }
  return (
    <Card title="Start here">
      <ol className="dash-start">
        <li>
          <Link to="/documents">Upload a statement or an export from your bank</Link>
          <span className="muted">
            {" "}
            (PDF, CSV, OFX, a photo). PerFin reads the account, its balance and its rows and offers to create the account for
            you.
          </span>
        </li>
        <li>
          <Link to="/accounts">Or add an account by hand</Link>
          <span className="muted"> for cash, a property, a loan or anything that has no statement.</span>
        </li>
        <li>
          <Link to="/review">Check the review queue</Link>
          <span className="muted"> for anything PerFin was not sure about. Each answer is remembered for the next file.</span>
        </li>
        <li>
          <Link to="/settings/categories">Name your payees and their usual category</Link>
          <span className="muted">
            , or write <Link to="/rules">rules</Link>, so later imports arrive categorised.
          </span>
        </li>
        <li>
          <Link to="/budgets">Set a budget</Link>
          <span className="muted">
            {" "}
            or a <Link to="/goals">goal</Link> once a month or two of spending is in.
          </span>
        </li>
      </ol>
    </Card>
  );
}
