// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";
import "./documents.css";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { Table } from "../../components/Table";
import { errorMessage } from "../../lib/api/client";
import { formatDateTime } from "../../lib/dates";
import { STATUS_LABELS } from "../imports/api";
import { statusTone } from "../imports/UploadPage";
import {
  docKeys,
  EMPTY_FILTERS,
  isKind,
  KIND_LABELS,
  listDocuments,
  SOURCE_LABELS,
  type DocumentFilters,
  type DocumentRow,
  type DocumentStatus,
  type SnippetPart,
} from "./api";

const STATUS_OPTIONS = [
  { value: "", label: "Any status" },
  ...Object.entries(STATUS_LABELS).map(([value, label]) => ({ value, label })),
];
const KIND_OPTIONS = [
  { value: "", label: "Any kind" },
  ...Object.entries(KIND_LABELS).map(([value, label]) => ({ value, label })),
];

function isStatus(value: string): value is DocumentStatus {
  return Object.keys(STATUS_LABELS).includes(value);
}

/** Matched words of a search hit, as text nodes and mark elements (never markup). */
export function Snippet({ parts }: { parts: readonly SnippetPart[] }) {
  return (
    <span className="ledger-muted">
      {parts.map((part, index) =>
        part.match ? (
          <mark key={index} className="snippet-hit">
            {part.text}
          </mark>
        ) : (
          <span key={index}>{part.text}</span>
        ),
      )}
    </span>
  );
}

export function DocumentsPage() {
  const [draft, setDraft] = useState<DocumentFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<DocumentFilters>(EMPTY_FILTERS);
  const query = useInfiniteQuery({
    queryKey: docKeys.list(applied),
    queryFn: ({ pageParam }) => listDocuments(applied, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const searching = applied.q.trim() !== "";
  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Document library</h1>
        <Link to="/documents">Upload files</Link>
      </div>
      <form
        className="ledger-filters"
        role="search"
        aria-label="Search documents"
        onSubmit={(event) => {
          event.preventDefault();
          setApplied(draft);
        }}
      >
        <Field label="Search the text of documents">
          <Input
            type="search"
            value={draft.q}
            onChange={(event) => {
              setDraft({ ...draft, q: event.target.value });
            }}
          />
        </Field>
        <Field label="Kind">
          <Select
            options={KIND_OPTIONS}
            value={draft.kind}
            onChange={(event) => {
              const value = event.target.value;
              setDraft({ ...draft, kind: isKind(value) ? value : "" });
            }}
          />
        </Field>
        <Field label="Status">
          <Select
            options={STATUS_OPTIONS}
            value={draft.status}
            onChange={(event) => {
              const value = event.target.value;
              setDraft({ ...draft, status: isStatus(value) ? value : "" });
            }}
          />
        </Field>
        <Field label="Tax year">
          <Input
            inputMode="numeric"
            value={draft.taxYear}
            onChange={(event) => {
              setDraft({ ...draft, taxYear: event.target.value });
            }}
          />
        </Field>
        <div className="ledger-actions">
          <Button type="submit" variant="primary">
            Search
          </Button>
          <Button
            onClick={() => {
              setDraft(EMPTY_FILTERS);
              setApplied(EMPTY_FILTERS);
            }}
          >
            Clear
          </Button>
        </div>
      </form>
      <Card title={searching ? "Search results" : "Documents"}>
        {query.isPending ? <Spinner label="Loading documents" /> : null}
        {query.isError ? (
          <p role="alert" className="ledger-error">
            {errorMessage(query.error)}
          </p>
        ) : null}
        {query.isSuccess ? (
          <Table<DocumentRow>
            caption="Documents"
            captionHidden
            rows={rows}
            rowKey={(row) => row.id}
            empty={searching ? "No document matches that search." : "No documents yet."}
            columns={[
              {
                id: "name",
                header: "File",
                rowHeader: true,
                cell: (row) => (
                  <span>
                    <Link to="/documents/$documentId" params={{ documentId: row.id }}>
                      {row.original_filename ?? "Unnamed file"}
                    </Link>
                    {row.snippet !== undefined && row.snippet !== null && row.snippet.length > 0 ? (
                      <>
                        <br />
                        <Snippet parts={row.snippet} />
                      </>
                    ) : null}
                  </span>
                ),
              },
              { id: "kind", header: "Kind", cell: (row) => KIND_LABELS[row.kind] },
              { id: "source", header: "Source", cell: (row) => SOURCE_LABELS[row.source] },
              {
                id: "status",
                header: "Status",
                cell: (row) => <Badge tone={statusTone(row.status)}>{STATUS_LABELS[row.status]}</Badge>,
              },
              { id: "year", header: "Tax year", cell: (row) => (row.tax_year === null ? "" : String(row.tax_year)) },
              { id: "when", header: "Received", cell: (row) => formatDateTime(row.created_at) ?? row.created_at },
            ]}
          />
        ) : null}
        {query.hasNextPage ? (
          <Button
            loading={query.isFetchingNextPage}
            onClick={() => {
              void query.fetchNextPage();
            }}
          >
            Load more
          </Button>
        ) : null}
      </Card>
    </section>
  );
}
