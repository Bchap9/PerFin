// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Imported recently" on Inbox, Needs you (spec section 4.3): the last ten
 * documents finished in the last fourteen days, imported or failed, from
 * `GET /documents?limit=25` filtered here. Each row names its file (a link),
 * its account (a link, when the person can see it), its period and, when the
 * read was not checked against a statement balance, says so quietly. A file
 * that failed says it could not be read.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { QuietNote } from "../../components/QuietNote";
import { Spinner } from "../../components/Spinner";
import { formatDate } from "../../lib/dates";
import { accountLabel, useAccounts, type Account } from "../accounts/api";
import {
  importKeys,
  listRecentDocuments,
  readAsActivity,
  readAsPositions,
  type DocumentRow,
} from "../imports/api";

/** How far back "recently" reaches, in days. */
const RECENT_DAYS = 14;

/** At most this many rows are shown. */
const RECENT_MAX = 10;

const DAY_MS = 86_400_000;

/** The documents finished (imported or failed) in the last fourteen days before `now`, newest first, at most ten. */
export function recentImports(rows: readonly DocumentRow[], now: Date): DocumentRow[] {
  const since = now.getTime() - RECENT_DAYS * DAY_MS;
  return rows
    .filter((row) => row.status === "processed" || row.status === "failed")
    .filter((row) => {
      const finished = Date.parse(row.updated_at);
      return Number.isFinite(finished) && finished >= since;
    })
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, RECENT_MAX);
}

function day(iso: string): string {
  return formatDate(iso) ?? iso;
}

function periodText(row: DocumentRow): string | null {
  const { period_start: start, period_end: end } = row;
  if (start !== null && end !== null) {
    return `${day(start)} to ${day(end)}`;
  }
  const only = end ?? start;
  return only === null ? null : day(only);
}

function RecentRow({ row, account }: { row: DocumentRow; account: Account | undefined }) {
  const name = row.original_filename ?? "Unnamed file";
  const period = periodText(row);
  const investment = readAsActivity(row) || readAsPositions(row);
  return (
    <li className="recent-imports__row">
      <Link to="/inbox/documents/$documentId" params={{ documentId: row.id }} className="recent-imports__name">
        {name}
      </Link>
      <span className="recent-imports__meta">
        {row.status === "failed" ? <span className="recent-imports__failed">Could not be read</span> : null}
        {/* Only an account the person can see is named (the list holds no others). */}
        {account === undefined ? null : (
          <Link to="/accounts/$accountId" params={{ accountId: account.id }}>
            {accountLabel(account)}
          </Link>
        )}
        {period === null ? null : <span>{period}</span>}
        {row.status === "processed" && investment ? (
          <span>{readAsPositions(row) ? "Read as investment holdings" : "Read as investment activity"}</span>
        ) : null}
        {row.status === "processed" && row.unchecked ? <QuietNote>Not checked against a balance</QuietNote> : null}
        {/* Investment activity and holdings, and files that are not statements, have no rows in the cash ledger. */}
        {row.status === "processed" && row.kind === "statement" && !investment ? (
          <Link to="/transactions" search={{ document: row.id }} aria-label={`View the rows of ${name}`}>
            View the rows
          </Link>
        ) : null}
      </span>
    </li>
  );
}

export function RecentImports() {
  const accounts = useAccounts();
  // Statuses change through the event stream (lib/sse.ts invalidates ["documents"]).
  const documents = useQuery({ queryKey: importKeys.documents, queryFn: () => listRecentDocuments() });
  const [now] = useState(() => new Date());
  const rows = useMemo(() => recentImports(documents.data ?? [], now), [documents.data, now]);
  const accountById = useMemo(
    () => new Map((accounts.data ?? []).map((account) => [account.id, account])),
    [accounts.data],
  );

  return (
    <Card
      title="Imported recently"
      className="recent-imports"
      actions={
        <Link to="/inbox/documents" className="link-action">
          All documents
        </Link>
      }
    >
      {documents.isPending ? <Spinner label="Loading recent imports" /> : null}
      {documents.isError ? (
        <p role="alert" className="inbox-error">
          Could not load the recent imports.{" "}
          <Button size="sm" variant="ghost" onClick={() => void documents.refetch()} aria-label="Retry loading the recent imports">
            Retry
          </Button>
        </p>
      ) : null}
      {documents.isSuccess && rows.length === 0 ? (
        <p className="muted">Nothing was imported in the last two weeks.</p>
      ) : null}
      {rows.length === 0 ? null : (
        <ul className="recent-imports__list">
          {rows.map((row) => (
            <RecentRow
              key={row.id}
              row={row}
              account={row.account_id === null ? undefined : accountById.get(row.account_id)}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}
