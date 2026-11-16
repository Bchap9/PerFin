// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Add documents on Inbox, Needs you (spec sections 1.3 and 4.3): a drop zone,
 * then one row per file sent from this page. The page's Upload button opens
 * the same file picker (`inputRef`).
 *
 * - The drop zone is a `<label>` over an `<input type="file" multiple>`, so a
 *   keyboard and a screen reader reach it like any file field.
 * - Each file uploads with progress (XHR, `imports/api.ts::uploadDocument`).
 * - A finished row links to its document (`/inbox/documents/<id>`). When the
 *   file waits on a person it says "Waiting on you above" and links to its
 *   card; once read it says whether it was imported.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type DragEvent, type RefObject } from "react";

import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import {
  importKeys,
  isAbort,
  listRecentDocuments,
  STATUS_LABELS,
  uploadDocument,
  type DocumentStatus,
} from "../imports/api";
import { listReviewCards, reviewKeys } from "../review/api";
import { documentAnchor } from "../review/ReviewPage";

export interface UploadItem {
  id: number;
  name: string;
  progress: number;
  state: "uploading" | "done" | "error";
  message: string | null;
  /** The document the file became, once the server has received it. */
  documentId: string | null;
}

/** What a finished upload says, from the cards waiting on a person and the documents' statuses. */
function DoneText({
  item,
  waiting,
  statuses,
}: {
  item: UploadItem;
  waiting: ReadonlySet<string>;
  statuses: ReadonlyMap<string, DocumentStatus>;
}) {
  if (item.message !== null) {
    return <span>{item.message}</span>;
  }
  if (item.documentId !== null && waiting.has(item.documentId)) {
    // To the whole Inbox, so the card is on the page even under `?document=`.
    return (
      <Link to="/inbox" hash={documentAnchor(item.documentId)} className="link-action">
        Waiting on you above
      </Link>
    );
  }
  const status = item.documentId === null ? undefined : statuses.get(item.documentId);
  if (status === "processed") {
    return <span>Imported</span>;
  }
  if (status === "failed") {
    return <span>Could not be read</span>;
  }
  if (status === "rejected") {
    return <span>{STATUS_LABELS.rejected}</span>;
  }
  return <span>Uploaded</span>;
}

/**
 * `inputRef` is the file input, for an Upload button elsewhere on the page
 * (the Inbox puts one at the top) to open the same picker.
 */
export function UploadZone({ inputRef }: { inputRef?: RefObject<HTMLInputElement> }) {
  const inputId = useId();
  const hintId = useId();
  const own = useRef<HTMLInputElement>(null);
  const input = inputRef ?? own;
  const canWrite = useCanWrite();
  const queryClient = useQueryClient();
  const accounts = useAccounts();
  const [accountId, setAccountId] = useState("");
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const counter = useRef(0);
  const sent = items.some((item) => item.documentId !== null);
  // The same reads as the cards and "Imported recently" (shared cache): they
  // say what became of each file. Statuses change through the event stream.
  const cards = useQuery({ queryKey: reviewKeys.cards(), queryFn: () => listReviewCards(), enabled: sent });
  const documents = useQuery({ queryKey: importKeys.documents, queryFn: () => listRecentDocuments(), enabled: sent });
  const waiting = useMemo(
    () => new Set((cards.data?.cards ?? []).filter((card) => card.open > 0).map((card) => card.document.id)),
    [cards.data],
  );
  const statuses = useMemo(
    () => new Map((documents.data ?? []).map((row) => [row.id, row.status])),
    [documents.data],
  );

  const patchItem = (id: number, change: Partial<UploadItem>): void => {
    setItems((current) => current.map((item) => (item.id === id ? { ...item, ...change } : item)));
  };

  const start = (files: readonly File[]): void => {
    for (const file of files) {
      counter.current += 1;
      const id = counter.current;
      setItems((current) => [
        ...current,
        { id, name: file.name, progress: 0, state: "uploading", message: null, documentId: null },
      ]);
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
            documentId: received.id,
            message:
              received.duplicate_of === null ? null : "This file was uploaded before, so it was not imported again.",
          });
          void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
          void queryClient.invalidateQueries({ queryKey: queryRoots.review });
          void queryClient.invalidateQueries({ queryKey: queryRoots.inbox });
        },
        (error: unknown) => {
          patchItem(id, { state: "error", message: isAbort(error) ? "Cancelled." : errorMessage(error) });
        },
      );
    }
  };

  // Files chosen with the page's Upload button: bring their rows into view.
  const uploads = useRef<HTMLUListElement>(null);
  const count = items.length;
  useEffect(() => {
    const list: { scrollIntoView?: unknown } | null = uploads.current;
    if (count > 0 && list !== null && typeof list.scrollIntoView === "function") {
      uploads.current?.scrollIntoView({ block: "nearest" });
    }
  }, [count]);

  const onDrop = (event: DragEvent<HTMLLabelElement>): void => {
    event.preventDefault();
    setDragging(false);
    start([...event.dataTransfer.files]);
  };

  if (!canWrite) {
    return (
      <Card title="Add documents" className="upload-zone-card">
        <p className="muted">You have read-only access.</p>
      </Card>
    );
  }

  const editable = (accounts.data ?? []).filter((account) => account.can_edit && !account.is_closed);

  return (
    <Card title="Add documents" className="upload-zone-card">
      <div className="upload-zone-card__body">
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
        <label
          htmlFor={inputId}
          className={`upload-zone${dragging ? " upload-zone--active" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => {
            setDragging(false);
          }}
          onDrop={onDrop}
        >
          <span className="upload-zone__title">Drop statements, exports, receipts or tax forms here</span>
          <span id={hintId} className="upload-zone__hint">
            PDF, CSV, TSV, XLSX, OFX, QFX, QIF, photos and scans, email files and ZIP archives.
          </span>
          <span className="btn btn--secondary btn--md upload-zone__choose">Choose files</span>
          <input
            ref={input}
            id={inputId}
            className="visually-hidden"
            type="file"
            multiple
            aria-label="Choose files"
            aria-describedby={hintId}
            onChange={(event) => {
              start([...(event.target.files ?? [])]);
              event.target.value = "";
            }}
          />
        </label>
        {items.length > 0 ? (
          <ul ref={uploads} className="upload-zone__items" aria-label="Uploads">
            {items.map((item) => (
              <li key={item.id} className="upload-zone__item">
                {item.documentId === null ? (
                  <span className="upload-zone__name">{item.name}</span>
                ) : (
                  <Link
                    to="/inbox/documents/$documentId"
                    params={{ documentId: item.documentId }}
                    className="upload-zone__name"
                  >
                    {item.name}
                  </Link>
                )}
                {item.state === "uploading" ? (
                  <progress aria-label={`Uploading ${item.name}`} max={1} value={item.progress} />
                ) : item.state === "done" ? (
                  <DoneText item={item} waiting={waiting} statuses={statuses} />
                ) : (
                  <span role="alert" className="inbox-error">
                    {item.message}
                  </span>
                )}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </Card>
  );
}
