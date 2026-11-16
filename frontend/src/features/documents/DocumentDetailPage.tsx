// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";
import "./documents.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { PageHeader } from "../../components/PageHeader";
import { QuietNote } from "../../components/QuietNote";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate, formatDateTime } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { investmentReading, STATUS_LABELS, statusTone } from "../imports/api";
import { DocumentAccountsDialog } from "../imports/DocumentAccountsDialog";
import {
  getReconciliation,
  reviewKeys,
  type StatementReconciliation,
} from "../review/api";
import { ReconcileView } from "../review/ReconcileView";
import {
  docKeys,
  downloadUrl,
  getDocumentDetail,
  getImported,
  KIND_LABELS,
  reprocessDocument,
  SOURCE_LABELS,
  type DocumentDetail,
  type ImportedStatement,
} from "./api";
import { DocumentLinksCard } from "./AttachDialog";
import { DeleteDocumentDialog, importedSummary } from "./DeleteDocumentDialog";
import { PageViewer } from "./PageViewer";
import { StatementSummaryCard } from "./StatementSummaryCard";
import { TeachDialog, currentReadings } from "../review/TeachDialog";

const NOT_CHECKED = "Not checked against a balance";
const NOT_CHECKED_WHY =
  "This statement prints no balances PerFin could check its rows against. The rows read cleanly and were imported. You can enter the balances, or remove a row that does not belong.";
/** The caption of the reconcile view's table of ledger rows (ReconcileView.tsx). */
const LEDGER_ROWS_CAPTION = "Rows of this statement in PerFin";

function day(iso: string): string {
  return formatDate(iso) ?? iso;
}

function periodText(statement: ImportedStatement): string | null {
  const { period_start: start, period_end: end } = statement;
  if (start !== null && end !== null) {
    return `${day(start)} to ${day(end)}`;
  }
  const only = end ?? start;
  return only === null ? null : day(only);
}

function rowsText(count: number): string {
  return count === 1 ? "1 row" : `${String(count)} rows`;
}

/** How the statement was checked, in words; nothing when there is nothing to say. */
function checkedText(statement: ImportedStatement): string | null {
  if (statement.verification === "reconciled") {
    return "Balanced against the statement";
  }
  if (statement.verification === "accepted") {
    return "Accepted without balancing";
  }
  return statement.unchecked ? NOT_CHECKED : null;
}

/**
 * Whether the reconcile view has something to do for the statement: it sums
 * the cash ledger's rows, so it is offered for a bank or card statement that
 * was imported without balancing (not checked, or accepted as read).
 */
function canBeChecked(statement: ImportedStatement): boolean {
  return (
    statement.kind === "bank" &&
    (statement.unchecked || statement.verification === "accepted")
  );
}

/**
 * The statement of the reconcile view that an imported statement is: the one
 * on its account with its period, or the only one on its account.
 */
export function matchStatement(
  statements: readonly StatementReconciliation[],
  target: Pick<ImportedStatement, "account_id" | "period_start" | "period_end">,
): StatementReconciliation | undefined {
  const onAccount = statements.filter(
    (entry) => entry.account_id === target.account_id,
  );
  const exact = onAccount.find(
    (entry) =>
      entry.period_start === target.period_start &&
      entry.period_end === target.period_end,
  );
  return exact ?? (onAccount.length === 1 ? onAccount[0] : undefined);
}

/**
 * The table row of the reconcile view that shows the `index`th ledger row.
 * The view draws one table row per ledger row, in order; when the table is
 * not there yet, or does not have exactly `total` rows, nothing is found.
 */
function ledgerRowElement(
  holder: HTMLElement | null,
  index: number,
  total: number,
): HTMLTableRowElement | null {
  for (const table of holder?.querySelectorAll("table") ?? []) {
    if (table.caption?.textContent.trim() !== LEDGER_ROWS_CAPTION) {
      continue;
    }
    const rows = table.tBodies.item(0)?.rows;
    return rows?.length === total ? rows.item(index) : null;
  }
  return null;
}

/**
 * The reconcile view for one imported statement. With `rowId` (a transaction
 * the person came from), that row is brought into view once.
 */
function StatementCheck({
  documentId,
  target,
  rowId,
  onDone,
}: {
  documentId: string;
  target: ImportedStatement;
  rowId: string;
  onDone: () => void;
}) {
  // The same query the view reads, so the statement is fetched once.
  const query = useQuery({
    queryKey: reviewKeys.reconciliation(documentId),
    queryFn: () => getReconciliation(documentId),
  });
  const holder = useRef<HTMLDivElement>(null);
  const shown = useRef(false);
  const found =
    query.data === undefined
      ? undefined
      : matchStatement(query.data.statements, target);
  const total = found?.rows.length ?? 0;
  const rowIndex =
    found === undefined || rowId === ""
      ? -1
      : found.rows.findIndex((row) => row.transaction_id === rowId);
  // Runs after every render until the row has been shown: the view draws its
  // table when its own read of the query settles.
  useEffect(() => {
    if (shown.current || rowIndex < 0) {
      return;
    }
    const element = ledgerRowElement(holder.current, rowIndex, total);
    if (element === null) {
      return;
    }
    shown.current = true;
    if (typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "center" });
    }
    element.focus({ preventScroll: true });
  });
  if (query.isPending) {
    return <Spinner label="Loading the statement" />;
  }
  if (found === undefined) {
    return (
      <div className="ledger-stack">
        <p role="alert" className="ledger-error">
          {query.isError
            ? errorMessage(query.error)
            : "This statement is not available to you."}
        </p>
        <div className="ledger-actions">
          <Button onClick={onDone}>Close</Button>
        </div>
      </div>
    );
  }
  return (
    <div ref={holder}>
      <ReconcileView
        documentId={documentId}
        statement={found.statement}
        onDone={onDone}
      />
    </div>
  );
}

/**
 * What the file produced: per statement the account it went to, its period,
 * its rows in the ledger and how it was checked. Shown to everyone who can
 * see the document; the reconcile view is offered to those who can change it.
 */
function ImportedCard({ doc, rowId }: { doc: DocumentDetail; rowId: string }) {
  const imported = useQuery({
    queryKey: docKeys.imported(doc.id),
    queryFn: () => getImported(doc.id),
  });
  // The position, in the list, of the statement being checked.
  const [checking, setChecking] = useState<number | null>(null);
  const data = imported.data;
  if (
    data === undefined ||
    (data.statements.length === 0 && data.skipped_lines === 0)
  ) {
    return null;
  }
  const statements = data.statements;
  // The explanation is long: it is said once, under the first marker.
  const explained = statements.findIndex((statement) => statement.unchecked);
  const target = checking === null ? undefined : statements.at(checking);
  return (
    <Card title="Imported from this file">
      <ul className="imported-list">
        {statements.map((statement, index) => {
          const period = periodText(statement);
          const checked = checkedText(statement);
          return (
            <li
              key={`${statement.kind}:${statement.account_id}:${String(index)}`}
              className="imported-statement"
            >
              <div className="imported-statement__facts">
                <Link
                  className="imported-statement__account"
                  to="/accounts/$accountId"
                  params={{ accountId: statement.account_id }}
                >
                  {statement.account_name}
                </Link>
                {period === null ? null : <span>{period}</span>}
                {statement.kind === "bank" ? (
                  <Link
                    to="/transactions"
                    search={{ document: doc.id, account: statement.account_id }}
                  >
                    {rowsText(statement.transactions)}
                  </Link>
                ) : (
                  <span>{rowsText(statement.transactions)}</span>
                )}
                {statement.pending > 0 ? (
                  <span>{statement.pending} waiting for review</span>
                ) : null}
                {checked === null ? null : statement.verification ===
                  "reconciled" ? (
                  <Badge tone="success">{checked}</Badge>
                ) : (
                  <span className="ledger-muted">{checked}</span>
                )}
              </div>
              {index === explained ? (
                <p className="imported-note">{NOT_CHECKED_WHY}</p>
              ) : null}
              {doc.can_edit && !doc.quarantined && canBeChecked(statement) ? (
                <Button
                  size="sm"
                  aria-expanded={checking === index}
                  onClick={() => {
                    setChecking(index);
                  }}
                >
                  Check against the statement
                </Button>
              ) : null}
            </li>
          );
        })}
      </ul>
      {data.skipped_lines > 0 ? (
        <p className="imported-note">
          {data.skipped_lines} line(s) were not read
        </p>
      ) : null}
      {target === undefined ? null : (
        <div className="imported-check">
          <StatementCheck
            // A different statement starts from its own figures.
            key={checking}
            documentId={doc.id}
            target={target}
            rowId={rowId}
            onDone={() => {
              setChecking(null);
            }}
          />
        </div>
      )}
    </Card>
  );
}

/**
 * The "Imported into" line of the details: each account the file's
 * statements went to (a link), what was imported in all, and the quiet
 * marker when a statement was not checked against a balance. Nothing when
 * no statement names an account.
 */
function ImportedInto({ documentId }: { documentId: string }) {
  // The same query as the "Imported from this file" card: fetched once.
  const imported = useQuery({
    queryKey: docKeys.imported(documentId),
    queryFn: () => getImported(documentId),
  });
  const data = imported.data;
  if (data === undefined || data.statements.length === 0) {
    return null;
  }
  const accounts = new Map<string, string>();
  for (const statement of data.statements) {
    if (!accounts.has(statement.account_id)) {
      accounts.set(statement.account_id, statement.account_name);
    }
  }
  const counts = importedSummary(data).join(", ");
  const unchecked = data.statements.some((statement) => statement.unchecked);
  return (
    <>
      <dt>Imported into</dt>
      <dd className="imported-into" data-testid="imported-into">
        <span className="imported-into__accounts">
          {[...accounts].map(([accountId, name], index) => (
            <span key={accountId}>
              {index === 0 ? null : ", "}
              <Link to="/accounts/$accountId" params={{ accountId }}>
                {name}
              </Link>
            </span>
          ))}
        </span>
        {counts === "" ? null : <span>{counts}</span>}
        {unchecked ? <QuietNote>{NOT_CHECKED}</QuietNote> : null}
      </dd>
    </>
  );
}

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
  const queryClient = useQueryClient();
  const toast = useToast();
  // A link from a transaction names its row (`?row=<transaction id>`).
  const search: Record<string, unknown> = useSearch({ strict: false });
  const rowId = typeof search["row"] === "string" ? search["row"] : "";
  const reprocess = useMutation({
    mutationFn: () => reprocessDocument(documentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      toast.notify({
        title: "Reading the file again",
        description: "Its status changes here when that is done.",
        tone: "success",
      });
    },
    onError: (error) => {
      toast.notify({
        title: "Could not process the file again",
        description: errorMessage(error),
        tone: "error",
      });
    },
  });
  const title = doc?.original_filename ?? "Document";
  // A brokerage export says how it was read, also while it waits on a person.
  const reading = doc === undefined ? null : investmentReading(doc);
  return (
    <div className="ledger-stack">
      <PageHeader
        title={title}
        breadcrumbs={[
          { to: "/inbox", label: "Inbox" },
          { to: "/inbox/documents", label: "Documents" },
          { label: title },
        ]}
      />
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
              <dd>
                {KIND_LABELS[doc.kind]}
                {reading === null ? null : (
                  <>
                    {" "}
                    <span className="ledger-muted">{reading}</span>
                  </>
                )}
              </dd>
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
              {doc.quarantined ? null : <ImportedInto documentId={doc.id} />}
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
                <Link to="/inbox" search={{ document: doc.id }}>
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
                  loading={reprocess.isPending}
                  onClick={() => {
                    reprocess.mutate();
                  }}
                >
                  Process again
                </Button>
              ) : null}
              {/* Teaching is done on the page; a file without pages has none. */}
              {doc.can_edit && !doc.quarantined && doc.previewable ? (
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
                  void navigate({ to: "/inbox/documents" });
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
          {doc.quarantined ? null : <ImportedCard doc={doc} rowId={rowId} />}
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
    </div>
  );
}
