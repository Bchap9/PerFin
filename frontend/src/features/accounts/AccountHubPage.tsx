// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The account hub (`/accounts/$accountId`): one page per account. The
 * header carries the breadcrumbs, the name, the type and how current the
 * balance is, the balance itself, the main action and a menu with Edit,
 * Close or Reopen, and Delete. The main action is "Add transaction", or for a
 * manual asset "Add valuation" (its Valuations tab, with the value field
 * ready, or the asset details with a first value when it has none), with
 * "Add transaction" moved to the menu. Below it the tabs (`?tab=`, see `hub.ts`):
 * Overview, Transactions and Documents for every account, plus the
 * investment panels, the loan's tabs or the asset's valuations by type.
 *
 * The loan and the asset are found from `GET /loans` and `GET /assets` by
 * `account_id`, only for the types that may carry one. Until that list is
 * in, the tabs wait, so a link to `?tab=schedule` never lands on Overview.
 */
import "./ledger.css";
import "./accounts.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { Menu, type MenuItem } from "../../components/Menu";
import { MoneyText } from "../../components/MoneyText";
import { PAGE_TITLE_ID, PageHeader } from "../../components/PageHeader";
import { Spinner } from "../../components/Spinner";
import { Stat, StatGroup } from "../../components/Stat";
import { Tabs } from "../../components/Tabs";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { useTabParam } from "../../lib/urlState";
import { AssetValuations } from "../assets/AssetValuations";
import { assetKeys, listAssets } from "../assets/api";
import { NewAssetDialog } from "../assets/NewAssetDialog";
import { HoldingsTab } from "../investments/HoldingsTab";
import { PerformanceTab } from "../investments/PerformanceTab";
import { TransactionsTab } from "../investments/TransactionsTab";
import { listLoans, loanKeys } from "../loans/api";
import { LoanPayments, LoanPayoff, LoanRates, LoanSchedule } from "../loans/LoanPanels";
import { NewLoanDialog } from "../loans/NewLoanDialog";
import { useRouteParam } from "../planning/useRouteParam";
import { TransactionDialog } from "../transactions/TransactionDialog";
import { AccountFormDialog, CloseAccountDialog } from "./AccountForms";
import { AccountOverview } from "./AccountOverview";
import { accountKeys, accountLabel, getAccount, reopenAccount, useCanWrite, type Account } from "./api";
import { DeleteAccountFlow, useCanDeleteAccount } from "./DeleteAccount";
import { freshness } from "./groups";
import { defaultTab, HUB_TAB_IDS, mayHaveAsset, mayHaveLoan, tabsFor, typeLabel, type HubTab } from "./hub";
import { HubDocuments } from "./HubDocuments";
import { HubTransactions } from "./HubTransactions";

type Opened = "edit" | "close" | "delete" | "transaction" | "terms" | "asset" | null;

function describeAccount(account: Account): string {
  const parts = [typeLabel(account), freshness(account, new Date())];
  if (account.is_closed) {
    parts.push(account.closed_on === null ? "Closed" : `Closed on ${formatDate(account.closed_on) ?? account.closed_on}`);
  }
  return parts.filter((part) => part !== "").join(" · ");
}

/** A panel in place of a tab whose object is missing: says why, and offers to add it to those who may. */
function Missing({ title, text, action }: { title: string; text: string; action?: { label: string; onClick: () => void } | undefined }) {
  return (
    <EmptyState
      title={title}
      description={text}
      {...(action === undefined
        ? {}
        : {
            action: (
              <Button variant="primary" onClick={action.onClick}>
                {action.label}
              </Button>
            ),
          })}
    />
  );
}

/**
 * A panel in place of a tab whose list could not be loaded: it says the
 * object is unknown rather than missing, and offers nothing to add, since
 * the object may well exist. Retry is in the alert above the tabs.
 */
function Unknown({ title }: { title: string }) {
  return <EmptyState title={title} description="Until they load, this tab cannot show whether this account has them. Use Retry above." />;
}

function Hub({ account }: { account: Account }) {
  const canWrite = useCanWrite();
  const canDelete = useCanDeleteAccount(account);
  const queryClient = useQueryClient();
  const toast = useToast();
  const [opened, setOpened] = useState<Opened>(null);
  const [requested, setTab] = useTabParam(HUB_TAB_IDS, defaultTab);
  // Bumped by "Add valuation" so the Valuations tab focuses its value field.
  const [valueRequest, setValueRequest] = useState(0);
  const writable = canWrite && account.can_edit;
  const name = accountLabel(account);

  const loanable = mayHaveLoan(account.type);
  const assetable = mayHaveAsset(account.type);
  const loans = useQuery({ queryKey: loanKeys.list, queryFn: listLoans, enabled: loanable });
  const assets = useQuery({ queryKey: assetKeys.list, queryFn: listAssets, enabled: assetable });
  const loan = loans.data?.find((entry) => entry.account_id === account.id);
  const asset = assets.data?.find((entry) => entry.account_id === account.id);
  const waiting = (loanable && loans.isPending) || (assetable && assets.isPending);

  const reopen = useMutation({
    mutationFn: () => reopenAccount(account.id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      toast.notify({ title: "Account reopened", tone: "success" });
    },
    onError: (caught) => {
      toast.notify({ title: "Could not reopen the account", description: errorMessage(caught), tone: "error" });
    },
  });

  const items: MenuItem[] = [];
  if (writable && assetable) {
    items.push({ id: "transaction", label: "Add transaction", onSelect: () => { setOpened("transaction"); } });
  }
  if (writable) {
    items.push({ id: "edit", label: "Edit", onSelect: () => { setOpened("edit"); } });
    items.push(
      account.is_closed
        ? { id: "reopen", label: "Reopen", disabled: reopen.isPending, onSelect: () => { reopen.mutate(); } }
        : { id: "close", label: "Close account", onSelect: () => { setOpened("close"); } },
    );
  }
  if (canDelete) {
    items.push({ id: "delete", label: "Delete account", danger: true, onSelect: () => { setOpened("delete"); } });
  }

  const tabs = tabsFor(account, {
    ...(loan === undefined ? {} : { loanId: loan.id }),
    ...(asset === undefined ? {} : { assetId: asset.id }),
  });
  const tab: HubTab = tabs.some((entry) => entry.id === requested) ? requested : defaultTab;

  const content = (id: HubTab) => {
    switch (id) {
      case "overview":
        return <AccountOverview account={account} writable={writable} />;
      case "transactions":
        return <HubTransactions account={account} />;
      case "documents":
        return <HubDocuments account={account} />;
      case "holdings":
        return (
          <div className="account-hub__panel">
            <HoldingsTab account={account.id} />
          </div>
        );
      case "activity":
        return <TransactionsTab account={account.id} />;
      case "performance":
        return <PerformanceTab account={account.id} />;
      case "schedule":
        if (loan === undefined && loans.isError) {
          return <Unknown title="The loan terms could not be loaded." />;
        }
        return loan === undefined ? (
          <Missing
            title="This account has no loan terms yet."
            text="Add them to see the schedule, record payments and plan extra payments."
            action={writable ? { label: "Add loan terms", onClick: () => { setOpened("terms"); } } : undefined}
          />
        ) : (
          <LoanSchedule loanId={loan.id} />
        );
      case "payments":
        return loan === undefined ? null : <LoanPayments loanId={loan.id} editable={writable} />;
      case "rates":
        return loan === undefined ? null : <LoanRates loanId={loan.id} editable={writable} />;
      case "payoff":
        return loan === undefined ? null : <LoanPayoff loanId={loan.id} />;
      case "valuations":
        if (asset === undefined && assets.isError) {
          return <Unknown title="The asset details could not be loaded." />;
        }
        return asset === undefined ? (
          <Missing
            title="This account has no asset details yet."
            text="Add them to record its value over time."
            action={writable ? { label: "Add asset details", onClick: () => { setOpened("asset"); } } : undefined}
          />
        ) : (
          <AssetValuations assetId={asset.id} focusValue={valueRequest} />
        );
    }
  };

  return (
    <section aria-label={name} className="account-hub">
      <PageHeader
        title={name}
        description={describeAccount(account)}
        breadcrumbs={[{ to: "/accounts", label: "Accounts" }, { label: name }]}
        actions={
          writable || items.length > 0 ? (
            <>
              {writable && assetable ? (
                <Button
                  disabled={assets.isPending}
                  onClick={() => {
                    if (asset === undefined && !assets.isError) {
                      setOpened("asset");
                      return;
                    }
                    setTab("valuations");
                    setValueRequest((count) => count + 1);
                  }}
                >
                  Add valuation
                </Button>
              ) : null}
              {writable && !assetable ? <Button onClick={() => { setOpened("transaction"); }}>Add transaction</Button> : null}
              {items.length > 0 ? <Menu label={`Actions for ${name}`} items={items} /> : null}
            </>
          ) : undefined
        }
      />
      <div className="account-hub__balance">
        <StatGroup label={`Balance of ${name}`}>
          <Stat label="Balance" size="display" value={<MoneyText amount={account.balance.amount} currency={account.balance.currency} />} />
        </StatGroup>
        {account.is_closed ? <Badge tone="neutral">Closed</Badge> : null}
      </div>
      {loans.isError ? (
        <p role="alert" className="ledger-error">
          The loan terms of this account could not be loaded: {errorMessage(loans.error)}{" "}
          <Button size="sm" onClick={() => void loans.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}
      {assets.isError ? (
        <p role="alert" className="ledger-error">
          The asset details of this account could not be loaded: {errorMessage(assets.error)}{" "}
          <Button size="sm" onClick={() => void assets.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}
      {waiting ? (
        <Spinner label="Loading the account's tabs" />
      ) : (
        <Tabs
          label={name}
          selectedId={tab}
          onSelect={(id) => {
            const next = tabs.find((entry) => entry.id === id);
            if (next !== undefined) {
              setValueRequest(0);
              setTab(next.id);
            }
          }}
          tabs={tabs.map((entry) => ({ id: entry.id, label: entry.label, content: content(entry.id) }))}
        />
      )}
      {opened === "edit" ? <AccountFormDialog open account={account} onClose={() => { setOpened(null); }} /> : null}
      {opened === "close" ? <CloseAccountDialog open account={account} onClose={() => { setOpened(null); }} /> : null}
      {opened === "delete" ? <DeleteAccountFlow account={account} onDone={() => { setOpened(null); }} /> : null}
      {opened === "transaction" ? <TransactionDialog defaultAccountId={account.id} onClose={() => { setOpened(null); }} /> : null}
      {opened === "terms" ? <NewLoanDialog account={account} onClose={() => { setOpened(null); }} /> : null}
      {opened === "asset" ? <NewAssetDialog account={account} onClose={() => { setOpened(null); }} /> : null}
    </section>
  );
}

export function AccountHubPage() {
  const accountId = useRouteParam("accountId");
  const account = useQuery({ queryKey: accountKeys.one(accountId), queryFn: () => getAccount(accountId) });
  if (account.isPending) {
    return (
      <div className="status-page">
        <Spinner size="lg" label="Loading account" />
      </div>
    );
  }
  if (account.isError) {
    return (
      <section className="ledger-stack" aria-labelledby={PAGE_TITLE_ID}>
        <h1 id={PAGE_TITLE_ID}>Account not found</h1>
        <p role="alert" className="ledger-error">
          {errorMessage(account.error)}
        </p>
        <Link to="/accounts">Back to accounts</Link>
      </section>
    );
  }
  return <Hub account={account.data} />;
}
