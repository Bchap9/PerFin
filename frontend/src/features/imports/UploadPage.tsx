// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useId, useRef, useState, type DragEvent } from "react";

import { Badge, type BadgeTone } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { Table } from "../../components/Table";
import { errorMessage } from "../../lib/api/client";
import { formatDateTime } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import {
  STATUS_LABELS,
  importKeys,
  isAbort,
  listRecentDocuments,
  uploadDocument,
  type DocumentRow,
  type DocumentStatus,
} from "./api";

export interface UploadItem {
  id: number;
  name: string;
  progress: number;
  state: "uploading" | "done" | "error";
  message: string | null;
}

export function statusTone(status: DocumentStatus): BadgeTone {
  switch (status) {
    case "processed":
      return "success";
    case "needs_review":
      return "warning";
    case "rejected":
    case "failed":
      return "danger";
    default:
      return "info";
  }
}

export function UploadPage() {
  const inputId = useId();
  const canWrite = useCanWrite();
  const queryClient = useQueryClient();
  const accounts = useAccounts();
  const [accountId, setAccountId] = useState("");
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const counter = useRef(0);
  // Statuses change through the event stream (lib/sse.ts invalidates ["documents"]).
  const documents = useQuery({ queryKey: importKeys.documents, queryFn: () => listRecentDocuments() });

  const patchItem = (id: number, change: Partial<UploadItem>): void => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...change } : item)));
  };

  const start = (files: readonly File[]): void => {
    for (const file of files) {
      counter.current += 1;
      const id = counter.current;
      setItems((current) => [...current, { id, name: file.name, progress: 0, state: "uploading", message: null }]);
      const handle = uploadDocument(file, {
        accountId: accountId === "" ? null : accountId,
        onProgress: (fraction) => {
          patchItem(id, { progress: fraction });
        },
      });
      handle.promise.then(
        (received) => {
          patchItem(id, {
            progress: 1,
            state: "done",
            message: received.duplicate_of === null ? null : "This file was uploaded before. It waits in the review queue.",
          });
          void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
          void queryClient.invalidateQueries({ queryKey: queryRoots.review });
        },
        (error: unknown) => {
          patchItem(id, { state: "error", message: isAbort(error) ? "Cancelled." : errorMessage(error) });
        },
      );
    }
  };

  const onDrop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragging(false);
    if (canWrite) {
      start([...event.dataTransfer.files]);
    }
  };

  const editable = (accounts.data ?? []).filter((account) => account.can_edit && !account.is_closed);

  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Documents</h1>
        <Link to="/documents/library">Document library and search</Link>
      </div>
      {canWrite ? (
        <Card title="Upload statements">
          <div className="ledger-stack">
            <Field label="Account" hint="Optional. Leave empty and PerFin tries to recognise the account.">
              <Select
                placeholder="Detect from the file"
                options={editable.map((account) => ({ value: account.id, label: accountLabel(account) }))}
                value={accountId}
                onChange={(event) => {
                  setAccountId(event.target.value);
                }}
              />
            </Field>
            <div
              className={`upload-drop${dragging ? " upload-drop--active" : ""}`}
              onDragOver={(event) => {
                event.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => {
                setDragging(false);
              }}
              onDrop={onDrop}
            >
              <p>Drop CSV, OFX, QIF or PDF files here</p>
              <label htmlFor={inputId} className="btn btn--secondary btn--md">
                Choose files
              </label>
              <input
                id={inputId}
                className="visually-hidden"
                type="file"
                multiple
                onChange={(event) => {
                  start([...(event.target.files ?? [])]);
                  event.target.value = "";
                }}
              />
            </div>
            {items.length > 0 ? (
              <ul className="account-list" aria-label="Uploads">
                {items.map((item) => (
                  <li key={item.id}>
                    <span>{item.name}</span>
                    {item.state === "uploading" ? (
                      <progress aria-label={`Uploading ${item.name}`} max={1} value={item.progress} />
                    ) : item.state === "done" ? (
                      <span>{item.message ?? "Uploaded"}</span>
                    ) : (
                      <span role="alert" className="ledger-error">
                        {item.message}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </Card>
      ) : (
        <p className="ledger-muted">You have read-only access.</p>
      )}
      <Card title="Recent uploads">
        {documents.isPending ? <Spinner label="Loading documents" /> : null}
        {documents.isError ? (
          <p role="alert" className="ledger-error">
            {errorMessage(documents.error)}
          </p>
        ) : null}
        {documents.isSuccess ? (
          <Table<DocumentRow>
            caption="Recent uploads"
            captionHidden
            maxHeight="md"
            rows={documents.data}
            rowKey={(row) => row.id}
            empty="Nothing uploaded yet."
            columns={[
              { id: "name", header: "File", rowHeader: true, cell: (row) => row.original_filename ?? "Unnamed file" },
              {
                id: "status",
                header: "Status",
                cell: (row) => <Badge tone={statusTone(row.status)}>{STATUS_LABELS[row.status]}</Badge>,
              },
              { id: "when", header: "Received", cell: (row) => formatDateTime(row.created_at) ?? row.created_at },
              {
                id: "next",
                header: "Next step",
                cell: (row) =>
                  row.status === "needs_review" && canWrite ? (
                    <span className="ledger-actions">
                      <Link to="/documents/$documentId/mapping" params={{ documentId: row.id }}>
                        Map columns
                      </Link>
                      <Link to="/review">Review</Link>
                    </span>
                  ) : null,
              },
            ]}
          />
        ) : null}
        <Button
          variant="ghost"
          onClick={() => {
            void documents.refetch();
          }}
        >
          Refresh
        </Button>
      </Card>
    </section>
  );
}
