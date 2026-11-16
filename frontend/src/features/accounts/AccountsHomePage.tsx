// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Accounts (`/accounts`): net worth on top, then every open account in one
 * list grouped by kind, each group with its total and each row saying how
 * current its balance is. Closed accounts fold away. `?group=` (the old
 * `/loans` and `/assets`) scrolls to a group and focuses its heading.
 */
import "./ledger.css";
import "./accounts.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { QuietNote } from "../../components/QuietNote";
import { Spinner } from "../../components/Spinner";
import { Stat, StatGroup } from "../../components/Stat";
import { errorMessage } from "../../lib/api/client";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { assetKeys, KIND_LABELS, listAssets, METHOD_LABELS, type Asset } from "../assets/api";
import { NewAssetDialog } from "../assets/NewAssetDialog";
import { formatFraction, labelOf } from "../investments/format";
import { listLoans, loanKeys, type Loan } from "../loans/api";
import { LOAN_ACCOUNT_TYPES, NewLoanDialog } from "../loans/NewLoanDialog";
import { getLatest, netWorthKeys } from "../networth/api";
import { getNetWorthSeries, netWorthChange, netWorthChangeWords, netWorthSeriesKey } from "../networth/monthChange";
import { AccountFormDialog } from "./AccountForms";
import { accountLabel, useAccounts, useCanWrite, type Account } from "./api";
import { freshness, GROUP_IDS, GROUPS, groupAccounts, type AccountGroupView, type GroupId } from "./groups";

function NetWorthHeader({ canWrite, onAdd }: { canWrite: boolean; onAdd: () => void }) {
  const latest = useQuery({ queryKey: netWorthKeys.latest, queryFn: getLatest });
  // The same month-to-date change as Home, from the same series.
  const series = useQuery({ queryKey: netWorthSeriesKey, queryFn: getNetWorthSeries });
  const change =
    latest.data === undefined || series.data === undefined
      ? null
      : netWorthChange(latest.data.net.amount, series.data.points, todayIsoDate());
  return (
    <section className="card accounts-worth" aria-label="Net worth">
      <div className="accounts-worth__figure">
        <StatGroup>
          <Stat
            label="Net worth"
            size="display"
            value={
              latest.data === undefined ? (
                latest.isPending ? (
                  <Spinner label="Loading net worth" />
                ) : (
                  "—"
                )
              ) : (
                <MoneyText amount={latest.data.net.amount} currency={latest.data.net.currency} tone="none" />
              )
            }
            {...(change === null || latest.data === undefined ? {} : { change: netWorthChangeWords(change, latest.data.currency) })}
          />
        </StatGroup>
        {latest.isError ? (
          <p role="alert" className="ledger-error">
            Net worth could not be loaded: {errorMessage(latest.error)}{" "}
            <Button size="sm" onClick={() => void latest.refetch()}>
              Retry
            </Button>
          </p>
        ) : null}
        {latest.data?.scope === "visible_accounts" ? <QuietNote>Of the accounts you can see, not the whole household.</QuietNote> : null}
        {latest.data !== undefined && latest.data.unconverted.length > 0 ? (
          <QuietNote>{`${String(latest.data.unconverted.length)} account(s) have no exchange rate and are left out.`}</QuietNote>
        ) : null}
      </div>
      <div className="accounts-worth__actions">
        <Link to="/reports/net-worth" className="link-action">
          Net worth over time
        </Link>
        {canWrite ? (
          <Button variant="primary" onClick={onAdd}>
            Add account
          </Button>
        ) : null}
      </div>
    </section>
  );
}

function loanLine(loan: Loan): string {
  const parts = [
    formatFraction(loan.rate, 3),
    loan.lender,
    loan.projection.next_due_date === null ? null : `next due ${formatDate(loan.projection.next_due_date) ?? loan.projection.next_due_date}`,
    loan.projection.payoff_date === null ? null : `payoff ${formatDate(loan.projection.payoff_date) ?? loan.projection.payoff_date}`,
  ];
  return parts.filter((part): part is string => part !== null && part !== "").join(" · ");
}

function assetLine(asset: Asset): string {
  return `${labelOf(KIND_LABELS, asset.kind)} · ${labelOf(METHOD_LABELS, asset.valuation_method)}`;
}

interface Extras {
  loans: ReadonlyMap<string, Loan> | null;
  assets: ReadonlyMap<string, Asset> | null;
  canWrite: boolean;
  now: Date;
  onAddTerms: (account: Account) => void;
  onAddAsset: (account: Account) => void;
}

/** The loan's or asset's details under its account, or the offer to add them. */
function RowDetails({ account, extras }: { account: Account; extras: Extras }) {
  const name = accountLabel(account);
  const mayAdd = extras.canWrite && account.can_edit && !account.is_closed;
  if (LOAN_ACCOUNT_TYPES.has(account.type) && extras.loans !== null) {
    const loan = extras.loans.get(account.id);
    if (loan !== undefined) {
      return (
        <span className="account-row__detail">
          <span>{loanLine(loan)}</span>{" "}
          <Link to="/accounts/$accountId" params={{ accountId: account.id }} search={{ tab: "schedule" }} aria-label={`Schedule of ${name}`}>
            Schedule
          </Link>
        </span>
      );
    }
    return mayAdd ? (
      <span className="account-row__detail">
        <button type="button" className="account-row__link" aria-label={`Add loan terms for ${name}`} onClick={() => { extras.onAddTerms(account); }}>
          Add loan terms
        </button>
      </span>
    ) : null;
  }
  if (account.type === "manual_asset" && extras.assets !== null) {
    const asset = extras.assets.get(account.id);
    if (asset !== undefined) {
      return (
        <span className="account-row__detail">
          <span>{assetLine(asset)}</span>{" "}
          <Link to="/accounts/$accountId" params={{ accountId: account.id }} search={{ tab: "valuations" }} aria-label={`Valuations of ${name}`}>
            Valuations
          </Link>
        </span>
      );
    }
    return mayAdd ? (
      <span className="account-row__detail">
        <button type="button" className="account-row__link" aria-label={`Add asset details for ${name}`} onClick={() => { extras.onAddAsset(account); }}>
          Add asset details
        </button>
      </span>
    ) : null;
  }
  return null;
}

function AccountRow({ account, extras }: { account: Account; extras: Extras }) {
  return (
    <li className="account-row">
      <span className="account-row__main">
        <span className="account-row__name">
          <Link to="/accounts/$accountId" params={{ accountId: account.id }}>
            {accountLabel(account)}
          </Link>
          {account.number_last4 === null ? null : <span className="ledger-muted"> ····{account.number_last4}</span>}
          {account.subtype_label === null ? null : <span className="ledger-muted"> · {account.subtype_label}</span>}
        </span>
        <span className="account-row__meta">
          {account.is_closed ? (
            <Badge tone="neutral">{account.closed_on === null ? "Closed" : `Closed on ${formatDate(account.closed_on) ?? account.closed_on}`}</Badge>
          ) : (
            <span>{freshness(account, extras.now)}</span>
          )}
          {account.include_in_net_worth ? null : <QuietNote>Not in net worth</QuietNote>}
        </span>
        <RowDetails account={account} extras={extras} />
      </span>
      <MoneyText className="account-row__balance" amount={account.balance.amount} currency={account.balance.currency} />
    </li>
  );
}

function GroupPanel({
  group,
  extras,
  headingRef,
}: {
  group: AccountGroupView;
  extras: Extras;
  headingRef: (node: HTMLHeadingElement | null) => void;
}) {
  const headingId = `account-group-${group.id}`;
  return (
    <section className="card accounts-group" id={`group-${group.id}`} aria-labelledby={headingId}>
      <header className="card__header accounts-group__header">
        <h2 id={headingId} className="card__title" tabIndex={-1} ref={headingRef}>
          {group.label}
        </h2>
        <span className="accounts-group__total">
          {group.accounts.length === 0 ? null : group.total === null ? (
            <span className="ledger-muted">Mixed currencies</span>
          ) : (
            <>
              <span className="visually-hidden">Total </span>
              <MoneyText amount={group.total.amount} currency={group.total.currency} tone="none" />
            </>
          )}
        </span>
      </header>
      {group.accounts.length === 0 ? (
        <p className="card__body ledger-muted">No open accounts of this kind yet.</p>
      ) : (
        <ul className="account-rows" aria-label={group.label}>
          {group.accounts.map((account) => (
            <AccountRow key={account.id} account={account} extras={extras} />
          ))}
        </ul>
      )}
    </section>
  );
}

function byAccount<T extends { account_id: string }>(items: readonly T[] | undefined): ReadonlyMap<string, T> | null {
  return items === undefined ? null : new Map(items.map((item) => [item.account_id, item]));
}

/** `groups` with an empty view of `wanted` in its place when it has no open account. */
function groupsWith(groups: AccountGroupView[], wanted: GroupId | null): AccountGroupView[] {
  if (wanted === null || groups.some((group) => group.id === wanted)) {
    return groups;
  }
  return GROUPS.flatMap((definition) => {
    const found = groups.find((group) => group.id === definition.id);
    if (found !== undefined) {
      return [found];
    }
    return definition.id === wanted ? [{ id: definition.id, label: definition.label, total: null, accounts: [] }] : [];
  });
}

function isGroupId(value: unknown): value is GroupId {
  return GROUP_IDS.some((id) => id === value);
}

export function AccountsHomePage() {
  const canWrite = useCanWrite();
  const query = useAccounts();
  const search: Record<string, unknown> = useSearch({ strict: false });
  const wanted = new Map(Object.entries(search)).get("group");
  const target = isGroupId(wanted) ? wanted : null;
  const [creating, setCreating] = useState(false);
  const [addingTerms, setAddingTerms] = useState<Account | null>(null);
  const [addingAsset, setAddingAsset] = useState<Account | null>(null);
  const headings = useRef(new Map<GroupId, HTMLHeadingElement>());
  const focused = useRef<GroupId | null>(null);

  const all = query.data ?? [];
  // The group the address names stays on the page even when it is empty, so
  // an old `/loans` or `/assets` bookmark lands on a heading that says so.
  const groups = groupsWith(groupAccounts(all), query.isSuccess ? target : null);
  const closed = all.filter((account) => account.is_closed).sort((a, b) => a.name.localeCompare(b.name));
  const hasLoans = all.some((account) => LOAN_ACCOUNT_TYPES.has(account.type));
  const hasAssets = all.some((account) => account.type === "manual_asset");
  const loans = useQuery({ queryKey: loanKeys.list, queryFn: listLoans, enabled: hasLoans });
  const assets = useQuery({ queryKey: assetKeys.list, queryFn: listAssets, enabled: hasAssets });

  const extras: Extras = {
    loans: byAccount(loans.data),
    assets: byAccount(assets.data),
    canWrite,
    now: new Date(),
    onAddTerms: setAddingTerms,
    onAddAsset: setAddingAsset,
  };

  const shown = groups.some((group) => group.id === target);
  useEffect(() => {
    if (target === null || !shown || focused.current === target) {
      return;
    }
    const heading = headings.current.get(target);
    if (heading !== undefined) {
      focused.current = target;
      // jsdom has no scrollIntoView; browsers do.
      const scrollable: { scrollIntoView?: unknown } = heading;
      if (typeof scrollable.scrollIntoView === "function") {
        heading.scrollIntoView({ block: "start" });
      }
      heading.focus();
    }
  }, [target, shown]);

  return (
    <div className="accounts-home">
      <NetWorthHeader canWrite={canWrite} onAdd={() => { setCreating(true); }} />
      {query.isPending ? <Spinner label="Loading accounts" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}{" "}
          <Button size="sm" onClick={() => void query.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}
      {loans.isError ? (
        <p role="alert" className="ledger-error">
          The loan terms could not be loaded: {errorMessage(loans.error)}{" "}
          <Button size="sm" onClick={() => void loans.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}
      {assets.isError ? (
        <p role="alert" className="ledger-error">
          The asset details could not be loaded: {errorMessage(assets.error)}{" "}
          <Button size="sm" onClick={() => void assets.refetch()}>
            Retry
          </Button>
        </p>
      ) : null}
      {query.isSuccess && all.length === 0 ? (
        <Card>
          <EmptyState
            title="No accounts yet"
            description={
              canWrite ? "Add an account, then import a statement or enter transactions." : "No account has been shared with you yet."
            }
          />
        </Card>
      ) : null}
      {groups.map((group) => (
        <GroupPanel
          key={group.id}
          group={group}
          extras={extras}
          headingRef={(node) => {
            if (node === null) {
              headings.current.delete(group.id);
            } else {
              headings.current.set(group.id, node);
            }
          }}
        />
      ))}
      {closed.length === 0 ? null : (
        <details className="card accounts-closed">
          <summary className="accounts-closed__summary">{`Closed (${String(closed.length)})`}</summary>
          <ul className="account-rows" aria-label="Closed accounts">
            {closed.map((account) => (
              <AccountRow key={account.id} account={account} extras={extras} />
            ))}
          </ul>
        </details>
      )}
      {creating ? (
        <AccountFormDialog
          open
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
      {addingTerms === null ? null : (
        <NewLoanDialog
          account={addingTerms}
          onClose={() => {
            setAddingTerms(null);
          }}
        />
      )}
      {addingAsset === null ? null : (
        <NewAssetDialog
          account={addingAsset}
          onClose={() => {
            setAddingAsset(null);
          }}
        />
      )}
    </div>
  );
}
