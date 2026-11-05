// SPDX-License-Identifier: AGPL-3.0-or-later
import "./ledger.css";

import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { MoneyText } from "../../components/MoneyText";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { sumMoney } from "../../lib/money";
import { AccountFormDialog } from "./AccountForms";
import {
  accountLabel,
  groupAccounts,
  useAccounts,
  useCanWrite,
  type Account,
} from "./api";

/** The group's total when every account shares one currency; otherwise none. */
export function groupTotal(
  accounts: readonly Account[],
): { amount: string; currency: string } | null {
  const first = accounts[0];
  if (
    first === undefined ||
    accounts.some(
      (account) => account.balance.currency !== first.balance.currency,
    )
  ) {
    return null;
  }
  return sumMoney(
    accounts.map((account) => account.balance),
    first.balance.currency,
  );
}

export function AccountsPage() {
  const canWrite = useCanWrite();
  const query = useAccounts();
  const [showClosed, setShowClosed] = useState(false);
  const [creating, setCreating] = useState(false);

  const accounts = (query.data ?? []).filter(
    (account) => showClosed || !account.is_closed,
  );
  const groups = groupAccounts(accounts);
  const hasClosed = (query.data ?? []).some((account) => account.is_closed);

  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Accounts</h1>
        <div className="ledger-actions">
          {hasClosed ? (
            <Button
              variant="ghost"
              onClick={() => {
                setShowClosed((value) => !value);
              }}
            >
              {showClosed ? "Hide closed accounts" : "Show closed accounts"}
            </Button>
          ) : null}
          {canWrite ? (
            <Button
              variant="primary"
              onClick={() => {
                setCreating(true);
              }}
            >
              New account
            </Button>
          ) : null}
        </div>
      </div>
      {query.isPending ? <Spinner label="Loading accounts" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : null}
      {query.isSuccess && groups.length === 0 ? (
        <Card>
          <EmptyState
            title="No accounts yet"
            description={
              canWrite
                ? "Add an account, then import a statement or enter transactions."
                : "No account has been shared with you yet."
            }
          />
        </Card>
      ) : null}
      {groups.map((group) => {
        const total = groupTotal(group.accounts);
        return (
          <Card
            key={group.type}
            title={group.label}
            actions={
              total === null ? undefined : (
                <MoneyText
                  amount={total.amount}
                  currency={total.currency}
                  tone="none"
                />
              )
            }
          >
            <ul className="account-list" aria-label={group.label}>
              {group.accounts.map((account) => (
                <li key={account.id}>
                  <span>
                    <Link
                      to="/accounts/$accountId"
                      params={{ accountId: account.id }}
                    >
                      {accountLabel(account)}
                    </Link>
                    {account.number_last4 === null ? null : (
                      <span className="ledger-muted">
                        {" "}
                        ····{account.number_last4}
                      </span>
                    )}
                    {account.subtype_label === null ? null : (
                      <span className="ledger-muted">
                        {" "}
                        · {account.subtype_label}
                      </span>
                    )}
                    {account.is_closed ? (
                      <Badge tone="neutral">Closed</Badge>
                    ) : null}
                  </span>
                  <MoneyText
                    amount={account.balance.amount}
                    currency={account.balance.currency}
                  />
                </li>
              ))}
            </ul>
          </Card>
        );
      })}
      {creating ? (
        <AccountFormDialog
          open
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
    </section>
  );
}
