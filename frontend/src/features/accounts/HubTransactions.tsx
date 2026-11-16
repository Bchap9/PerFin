// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Transactions tab of the account hub: the account's latest 50
 * transactions. Each opens in the Transactions drawer
 * (`/transactions?account=<id>&tx=<id>`), where its category, how that
 * category was decided and every action live; the list ends with a link to
 * all of the account's transactions there. Categories are left out here, so
 * a guess is never shown without saying it is one.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import { MoneyText } from "../../components/MoneyText";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { EMPTY_FILTERS, listTransactions, merchantName, PAGE_SIZE, type Transaction } from "../transactions/api";
import { accountLabel, type Account } from "./api";

/** Under the transactions root (so every ledger change refreshes it), apart from the grid's own lists. */
const latestKey = (accountId: string) => [...queryRoots.transactions, "account-latest", accountId] as const;

function describe(row: Transaction): string {
  const name = merchantName(row) ?? (row.raw_payee ?? "").trim();
  return name === "" ? "Transaction" : name;
}

export function HubTransactions({ account }: { account: Account }) {
  const query = useQuery({
    queryKey: latestKey(account.id),
    queryFn: () => listTransactions({ ...EMPTY_FILTERS, account: account.id }, null),
  });
  const name = accountLabel(account);
  const rows = query.data?.items ?? [];
  const columns: TableColumn<Transaction>[] = [
    {
      id: "date",
      header: "Date",
      cell: (row) => formatDate(row.posted_date) ?? row.posted_date,
    },
    {
      id: "description",
      header: "Description",
      rowHeader: true,
      cell: (row) => (
        <span className="hub-tx__description">
          <Link to="/transactions" search={{ account: account.id, tx: row.id }}>
            {describe(row)}
          </Link>
          {row.status === "pending" ? <Badge tone="info">Pending</Badge> : null}
        </span>
      ),
    },
    {
      id: "amount",
      header: "Amount",
      align: "end",
      cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} />,
    },
  ];
  return (
    <div className="account-hub__panel">
      <Card
        title="Latest transactions"
        footer={
          <Link to="/transactions" search={{ account: account.id }} className="link-action">
            All transactions of this account
          </Link>
        }
      >
        {query.isPending ? <Spinner label="Loading transactions" /> : null}
        {query.isError ? (
          <p role="alert" className="ledger-error">
            Could not load the transactions. {errorMessage(query.error)}
          </p>
        ) : null}
        {query.isSuccess ? (
          <>
            <Table
              caption={`Latest transactions of ${name}`}
              captionHidden
              maxHeight="md"
              rows={rows}
              rowKey={(row) => row.id}
              empty="No transactions on this account yet."
              columns={columns}
            />
            {query.data.next_cursor === null ? null : (
              <p className="ledger-muted">The latest {String(PAGE_SIZE)} are shown; the rest are under all transactions.</p>
            )}
          </>
        ) : null}
      </Card>
    </div>
  );
}
