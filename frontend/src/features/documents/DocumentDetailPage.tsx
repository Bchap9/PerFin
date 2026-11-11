// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";
import "./documents.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { formatDate, formatDateTime } from "../../lib/dates";
import { STATUS_LABELS } from "../imports/api";
import { DocumentAccountsDialog } from "../imports/DocumentAccountsDialog";
import { statusTone } from "../imports/UploadPage";
import {
  docKeys,
  downloadUrl,
  getDocumentDetail,
  KIND_LABELS,
  SOURCE_LABELS,
} from "./api";
import { DocumentLinksCard } from "./AttachDialog";
import { DeleteDocumentDialog } from "./DeleteDocumentDialog";
import { PageViewer } from "./PageViewer";
import { StatementSummaryCard } from "./StatementSummaryCard";
import { TeachDialog, currentReadings } from "../review/TeachDialog";

export function DocumentDetailPage() {
  const documentId = useParams({
    strict: false,
    select: (params: Record<string, string | undefined>) =>
      params["documentId"] ?? "",
  });
  const detail = useQuery({
    queryKey: docKeys.one(documentId),
    queryFn: () => getDocumentDetail(documentId),
  });
  const doc = detail.data;
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [teaching, setTeaching] = useState(false);
  const navigate = useNavigate();
  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">{doc?.original_filename ?? "Document"}</h1>
        <Link to="/documents/library">Document library</Link>
      </div>
      {detail.isPending ? <Spinner label="Loading document" /> : null}
      {detail.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(detail.error)}
        </p>
      ) : null}
      {doc === undefined ? null : (
        <>
          <Card title="Details">
            <dl>
              <dt>Kind</dt>
              <dd>{KIND_LABELS[doc.kind]}</dd>
              <dt>Status</dt>
              <dd>
                <Badge tone={statusTone(doc.status)}>
                  {STATUS_LABELS[doc.status]}
                </Badge>
                {doc.quarantined ? (
                  <Badge tone="danger">Quarantined</Badge>
                ) : null}
              </dd>
              <dt>Source</dt>
              <dd>{SOURCE_LABELS[doc.source]}</dd>
              <dt>Received</dt>
              <dd>{formatDateTime(doc.created_at) ?? doc.created_at}</dd>
              {doc.period_start === null ? null : (
                <>
                  <dt>Period</dt>
                  <dd>
                    {formatDate(doc.period_start) ?? doc.period_start} to{" "}
                    {doc.period_end === null
                      ? ""
                      : (formatDate(doc.period_end) ?? doc.period_end)}
                  </dd>
                </>
              )}
              {doc.tax_year === null ? null : (
                <>
                  <dt>Tax year</dt>
                  <dd>{doc.tax_year}</dd>
                </>
              )}
              <dt>Size</dt>
              <dd>{Math.max(1, Math.round(doc.size / 1024))} KiB</dd>
            </dl>
            {doc.error === null ? null : (
              <p className="ledger-error">{doc.error}</p>
            )}
            <div className="ledger-actions">
              {doc.downloadable ? (
                <a
                  className="btn btn--secondary btn--md"
                  href={downloadUrl(doc.id)}
                  download
                >
                  Download original
                </a>
              ) : (
                <span className="ledger-muted">
                  Your role cannot download the original file.
                </span>
              )}
              {doc.open_review_items > 0 ? (
                <Link to="/review">
                  {doc.open_review_items} item(s) to review
                </Link>
              ) : null}
              {doc.can_edit && doc.kind === "statement" ? (
                <Button
                  onClick={() => {
                    setAccountsOpen(true);
                  }}
                >
                  Accounts in this file
                </Button>
              ) : null}
              {doc.can_edit && !doc.quarantined ? (
                <Button
                  onClick={() => {
                    setTeaching(true);
                  }}
                >
                  Teach PerFin
                </Button>
              ) : null}
              {doc.can_edit ? (
                <Button
                  variant="danger"
                  onClick={() => {
                    setDeleting(true);
                  }}
                >
                  Delete
                </Button>
              ) : null}
            </div>
            {deleting ? (
              <DeleteDocumentDialog
                documentId={doc.id}
                filename={doc.original_filename ?? "Unnamed file"}
                onClose={() => {
                  setDeleting(false);
                }}
                onDeleted={() => {
                  void navigate({ to: "/documents/library" });
                }}
              />
            ) : null}
            {teaching ? (
              <TeachDialog
                documentId={doc.id}
                readings={currentReadings(doc)}
                onClose={() => {
                  setTeaching(false);
                }}
              />
            ) : null}
            {accountsOpen ? (
              <DocumentAccountsDialog
                documentId={doc.id}
                onClose={() => {
                  setAccountsOpen(false);
                }}
              />
            ) : null}
          </Card>
          {doc.can_edit && doc.kind === "statement" && !doc.quarantined ? (
            <StatementSummaryCard documentId={doc.id} />
          ) : null}
          <DocumentLinksCard document={doc} />
          {doc.quarantined ? null : (
            <Card title="Pages">
              <PageViewer documentId={doc.id} />
            </Card>
          )}
        </>
      )}
    </section>
  );
}
