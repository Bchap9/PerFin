// SPDX-License-Identifier: AGPL-3.0-or-later
// Stage 3 bridge: lane A replaces this.
import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";

import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { AssetDetailPage } from "../assets/AssetDetailPage";
import { assetKeys, listAssets } from "../assets/api";
import { LoanDetailPage } from "../loans/LoanDetailPage";
import { listLoans, loanKeys } from "../loans/api";
import { useRouteParam } from "../planning/useRouteParam";
import { AccountDetailPage } from "./AccountDetailPage";

const LOAN_TABS = new Set(["schedule", "payments", "rates", "payoff"]);

function Missing({ title, text, error }: { title: string; text: string; error?: unknown }) {
  return (
    <section className="status-page" aria-labelledby="hub-missing-title">
      <h1 id="hub-missing-title">{title}</h1>
      <p>{error === undefined ? text : errorMessage(error)}</p>
      <Link to="/accounts" className="btn btn--primary btn--md">
        Go to Accounts
      </Link>
    </section>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="status-page">
      <Spinner size="lg" label={label} />
    </div>
  );
}

/** The loan of this account (from `GET /loans`), shown as today's loan page. */
function LoanTabs({ accountId }: { accountId: string }) {
  const loans = useQuery({ queryKey: loanKeys.list, queryFn: listLoans });
  if (loans.isPending) {
    return <Loading label="Loading the loan" />;
  }
  if (loans.isError) {
    return <Missing title="Could not open the loan" text="" error={loans.error} />;
  }
  const loan = loans.data.find((entry) => entry.account_id === accountId);
  return loan === undefined ? (
    <Missing title="No loan on this account" text="This account has no loan terms." />
  ) : (
    <LoanDetailPage loanId={loan.id} />
  );
}

/** The asset of this account (from `GET /assets`), shown as today's asset page. */
function ValuationTabs({ accountId }: { accountId: string }) {
  const assets = useQuery({ queryKey: assetKeys.list, queryFn: listAssets });
  if (assets.isPending) {
    return <Loading label="Loading the asset" />;
  }
  if (assets.isError) {
    return <Missing title="Could not open the asset" text="" error={assets.error} />;
  }
  const asset = assets.data.find((entry) => entry.account_id === accountId);
  return asset === undefined ? (
    <Missing title="No asset on this account" text="This account has no valuations." />
  ) : (
    <AssetDetailPage assetId={asset.id} />
  );
}

/**
 * The account hub (`/accounts/$accountId`). Until lane A builds the tabs:
 * a loan tab (`schedule`, `payments`, `rates`, `payoff`) shows only today's
 * loan page for this account's loan, `valuations` only today's asset page,
 * anything else today's account page.
 */
export function AccountHubPage() {
  const accountId = useRouteParam("accountId");
  const search: Record<string, unknown> = useSearch({ strict: false });
  const tab = typeof search["tab"] === "string" ? search["tab"] : "";
  if (LOAN_TABS.has(tab)) {
    return <LoanTabs accountId={accountId} />;
  }
  if (tab === "valuations") {
    return <ValuationTabs accountId={accountId} />;
  }
  return <AccountDetailPage />;
}
