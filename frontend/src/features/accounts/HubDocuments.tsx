// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Documents tab of the account hub: the files filed to this account
 * (`GET /documents?account=<id>`), newest first, each linking to its page
 * in the Inbox (`/inbox/documents/<id>`). A read that was not checked
 * against a statement balance says so.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { Badge, type BadgeTone } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { QuietNote } from "../../components/QuietNote";
import { Spinner } from "../../components/Spinner";
import { api, errorMessage, unwrap } from "../../lib/api/client";
import type { components } from "../../lib/api/schema";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, type Account } from "./api";

type DocumentItem = components["schemas"]["DocumentListItem"];
type DocumentKind = components["schemas"]["DocumentKind"];
type DocumentStatus = components["schemas"]["DocumentStatus"];

const PAGE = 50;

const documentsKey = (accountId: string) => [...queryRoots.documents, "account", accountId] as const;

const KINDS: ReadonlyMap<DocumentKind, string> = new Map<DocumentKind, string>([
  ["statement", "Statement"],
  ["receipt", "Receipt"],
  ["invoice", "Invoice"],
  ["tax_form", "Tax form"],
  ["payslip", "Payslip"],
  ["insurance", "Insurance"],
  ["other", "Other document"],
  ["unknown", "Document"],
]);

/** A status worth a word; a processed file says nothing. */
const STATUSES: ReadonlyMap<DocumentStatus, { label: string; tone: BadgeTone }> = new Map<DocumentStatus, { label: string; tone: BadgeTone }>([
  ["received", { label: "Received", tone: "neutral" }],
  ["scanning", { label: "Being read", tone: "neutral" }],
  ["extracting", { label: "Being read", tone: "neutral" }],
  ["classifying", { label: "Being read", tone: "neutral" }],
  ["parsing", { label: "Being read", tone: "neutral" }],
  ["needs_review", { label: "Needs review", tone: "warning" }],
  ["rejected", { label: "Rejected", tone: "danger" }],
  ["failed", { label: "Could not be read", tone: "danger" }],
]);

function kindLabel(kind: DocumentKind): string {
  return KINDS.get(kind) ?? "Document";
}

function title(item: DocumentItem): string {
  const name = (item.original_filename ?? "").trim();
  return name === "" ? `Untitled ${kindLabel(item.kind).toLowerCase()}` : name;
}

function period(item: DocumentItem): string | null {
  if (item.period_start !== null && item.period_end !== null) {
    return `${formatDate(item.period_start) ?? item.period_start} to ${formatDate(item.period_end) ?? item.period_end}`;
  }
  return item.tax_year === null ? null : `Tax year ${String(item.tax_year)}`;
}

function listDocuments(accountId: string, cursor: string | null) {
  return unwrap(
    api.GET("/api/v1/documents", {
      params: { query: { account: accountId, limit: PAGE, ...(cursor === null ? {} : { cursor }) } },
    }),
  );
}

export function HubDocuments({ account }: { account: Account }) {
  const query = useInfiniteQuery({
    queryKey: documentsKey(account.id),
    queryFn: ({ pageParam }) => listDocuments(account.id, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  const name = accountLabel(account);
  return (
    <div className="account-hub__panel">
      <Card title="Documents">
        {query.isPending ? <Spinner label="Loading documents" /> : null}
        {query.isError ? (
          <p role="alert" className="ledger-error">
            Could not load the documents. {errorMessage(query.error)}
          </p>
        ) : null}
        {query.isSuccess && items.length === 0 ? (
          <p className="ledger-muted">
            No documents are filed to this account. Upload a statement in the <Link to="/inbox" hash="inbox-add-documents">Inbox</Link>.
          </p>
        ) : null}
        {items.length === 0 ? null : (
          <ul className="hub-docs" aria-label={`Documents of ${name}`}>
            {items.map((item) => {
              const status = STATUSES.get(item.status);
              const when = period(item);
              return (
                <li key={item.id} className="hub-docs__item">
                  <Link to="/inbox/documents/$documentId" params={{ documentId: item.id }} className="hub-docs__name">
                    {title(item)}
                  </Link>
                  <span className="hub-docs__meta">
                    <span>{kindLabel(item.kind)}</span>
                    {when === null ? null : <span>{when}</span>}
                    {status === undefined ? null : <Badge tone={status.tone}>{status.label}</Badge>}
                    {item.unchecked ? <QuietNote>Not checked against a balance</QuietNote> : null}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        {query.hasNextPage ? (
          <Button
            size="sm"
            loading={query.isFetchingNextPage}
            onClick={() => {
              void query.fetchNextPage();
            }}
          >
            Show more documents
          </Button>
        ) : null}
      </Card>
    </div>
  );
}
