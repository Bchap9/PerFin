// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What PerFin has learned about this household's documents, to look at and to
 * take back: the layouts taught in the review queue ("Remember this layout",
 * "Teach PerFin"; `GET/DELETE /imports/layouts`) and the saved column
 * mappings (`/imports/mappings`). A layout or mapping learned from a wrong
 * answer would misread every later file of its kind, so each can be removed
 * here; the next file is then read the ordinary way and can be taught again.
 */
import "../accounts/ledger.css";

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { Table } from "../../components/Table";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import { useAction } from "../planning/useAction";
import {
  deleteLayout,
  deleteMapping,
  importKeys,
  learnedKeys,
  listLayouts,
  listMappings,
  renameMapping,
  type LearnedLayout,
  type Mapping,
} from "./api";
import { kindLabel, layoutReads, mappingReads } from "./learned";

const NAME_MAX = 200;

function day(value: string | null): string {
  return value === null ? "Never" : (formatDate(value.slice(0, 10)) ?? value);
}

function ForgetLayoutDialog({ layout, onClose }: { layout: LearnedLayout; onClose: () => void }) {
  const forget = useAction(() => deleteLayout(layout.id), {
    invalidate: [queryRoots.documents],
    success: "Layout forgotten",
  });
  return (
    <Dialog
      open
      size="sm"
      onClose={onClose}
      title={`Forget the ${layout.institution_name} layout?`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={forget.busy}
            onClick={() => {
              void forget.run().then((done) => {
                if (done !== undefined) {
                  onClose();
                }
              });
            }}
          >
            Forget this layout
          </Button>
        </>
      }
    >
      <p>
        PerFin stops using what it was taught about this layout. Files already imported keep their rows. The next{" "}
        {kindLabel(layout.kind).toLowerCase()} from {layout.institution_name} is read the ordinary way, and you can teach it again
        from the review queue.
      </p>
      {forget.error === null ? null : (
        <p role="alert" className="ledger-error">
          {forget.error}
        </p>
      )}
    </Dialog>
  );
}

function RenameMappingDialog({ mapping, onClose }: { mapping: Mapping; onClose: () => void }) {
  const [name, setName] = useState(mapping.name);
  const [problem, setProblem] = useState<string | null>(null);
  const save = useAction((next: string) => renameMapping(mapping.id, next), {
    invalidate: [queryRoots.documents],
    success: "Mapping renamed",
  });
  const submit = async (): Promise<void> => {
    const next = name.trim();
    if (next === "") {
      setProblem("Enter a name.");
      return;
    }
    setProblem(null);
    if ((await save.run(next)) !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      size="sm"
      onClose={onClose}
      title="Rename column mapping"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save name
          </Button>
        </>
      }
    >
      <Field label="Name" error={problem ?? save.error ?? undefined} required>
        <Input
          value={name}
          maxLength={NAME_MAX}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
    </Dialog>
  );
}

function DeleteMappingDialog({ mapping, onClose }: { mapping: Mapping; onClose: () => void }) {
  const remove = useAction(() => deleteMapping(mapping.id), {
    invalidate: [queryRoots.documents],
    success: "Mapping deleted",
  });
  return (
    <Dialog
      open
      size="sm"
      onClose={onClose}
      title={`Delete "${mapping.name}"?`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={remove.busy}
            onClick={() => {
              void remove.run().then((done) => {
                if (done !== undefined) {
                  onClose();
                }
              });
            }}
          >
            Delete mapping
          </Button>
        </>
      }
    >
      <p>
        Files with these columns are no longer imported by themselves. Rows already imported stay. The next such file asks
        for its columns again.
      </p>
      {remove.error === null ? null : (
        <p role="alert" className="ledger-error">
          {remove.error}
        </p>
      )}
    </Dialog>
  );
}

function LayoutsCard({ canWrite }: { canWrite: boolean }) {
  const layouts = useQuery({ queryKey: learnedKeys.layouts, queryFn: listLayouts });
  const [forgetting, setForgetting] = useState<LearnedLayout | null>(null);
  return (
    <Card title="Statement layouts">
      <p className="ledger-muted">
        Taught in the review queue with &ldquo;Remember this layout&rdquo; or &ldquo;Teach PerFin&rdquo;. PerFin keeps the words
        printed next to each value, never the values themselves.
      </p>
      {layouts.isPending ? <Spinner label="Loading layouts" /> : null}
      {layouts.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(layouts.error)}
        </p>
      ) : null}
      {layouts.isSuccess ? (
        <Table<LearnedLayout>
          caption="Learned statement layouts"
          captionHidden
          rows={layouts.data}
          rowKey={(row) => row.id}
          empty="Nothing taught yet. When a statement is misread, use Teach PerFin on its review item."
          columns={[
            {
              id: "institution",
              header: "Institution",
              rowHeader: true,
              cell: (row) => (
                <>
                  {row.institution_name}{" "}
                  <Badge tone={row.taught ? "success" : "info"}>{row.taught ? "Taught" : "Recognised"}</Badge>
                </>
              ),
            },
            { id: "kind", header: "Document", cell: (row) => kindLabel(row.kind) },
            {
              id: "recognised",
              header: "Recognised by",
              cell: (row) =>
                row.recognised_by.length === 0 ? "Its header" : row.recognised_by.map((phrase) => `"${phrase}"`).join(", "),
            },
            {
              id: "reads",
              header: "What it reads",
              cell: (row) => (
                <>
                  {layoutReads(row).map((line) => (
                    <div key={line}>{line}</div>
                  ))}
                  {row.notes.map((note) => (
                    <div key={note} className="ledger-muted">
                      Note: {note}
                    </div>
                  ))}
                </>
              ),
            },
            {
              id: "learned",
              header: "Learned",
              cell: (row) =>
                row.learned_from_document_id === null ? (
                  day(row.learned_at)
                ) : (
                  <Link to="/documents/$documentId" params={{ documentId: row.learned_from_document_id }}>
                    {day(row.learned_at)}
                  </Link>
                ),
            },
            ...(canWrite
              ? [
                  {
                    id: "actions",
                    header: <span className="visually-hidden">Actions</span>,
                    cell: (row: LearnedLayout) => (
                      <Button
                        size="sm"
                        aria-label={`Forget the ${row.institution_name} layout`}
                        onClick={() => {
                          setForgetting(row);
                        }}
                      >
                        Forget
                      </Button>
                    ),
                  },
                ]
              : []),
          ]}
        />
      ) : null}
      {forgetting === null ? null : (
        <ForgetLayoutDialog
          layout={forgetting}
          onClose={() => {
            setForgetting(null);
          }}
        />
      )}
    </Card>
  );
}

function MappingsCard({ canWrite }: { canWrite: boolean }) {
  const mappings = useQuery({ queryKey: importKeys.mappings, queryFn: listMappings });
  const accounts = useAccounts();
  const [renaming, setRenaming] = useState<Mapping | null>(null);
  const [deleting, setDeleting] = useState<Mapping | null>(null);
  const accountName = (id: string | null): string => {
    if (id === null) {
      return "Any account";
    }
    const account = (accounts.data ?? []).find((candidate) => candidate.id === id);
    return account === undefined ? "An account you cannot see" : accountLabel(account);
  };
  return (
    <Card title="Column mappings">
      <p className="ledger-muted">
        Saved when a spreadsheet or table names its columns plainly, or when you map them once. A file with the same header
        row is then imported without a question.
      </p>
      {mappings.isPending ? <Spinner label="Loading column mappings" /> : null}
      {mappings.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(mappings.error)}
        </p>
      ) : null}
      {mappings.isSuccess ? (
        <Table<Mapping>
          caption="Saved column mappings"
          captionHidden
          rows={mappings.data}
          rowKey={(row) => row.id}
          empty="No column mappings saved yet."
          columns={[
            { id: "name", header: "Name", rowHeader: true, cell: (row) => row.name },
            { id: "account", header: "Imports into", cell: (row) => accountName(row.account_id) },
            { id: "reads", header: "How it reads", cell: (row) => mappingReads(row) },
            {
              id: "used",
              header: "Used",
              cell: (row) =>
                row.use_count === 0
                  ? "Not yet"
                  : `${String(row.use_count)} ${row.use_count === 1 ? "time" : "times"}, last ${day(row.last_used_at)}`,
            },
            ...(canWrite
              ? [
                  {
                    id: "actions",
                    header: <span className="visually-hidden">Actions</span>,
                    cell: (row: Mapping) => (
                      <span className="ledger-actions">
                        <Button
                          size="sm"
                          aria-label={`Rename ${row.name}`}
                          onClick={() => {
                            setRenaming(row);
                          }}
                        >
                          Rename
                        </Button>
                        <Button
                          size="sm"
                          aria-label={`Delete ${row.name}`}
                          onClick={() => {
                            setDeleting(row);
                          }}
                        >
                          Delete
                        </Button>
                      </span>
                    ),
                  },
                ]
              : []),
          ]}
        />
      ) : null}
      {renaming === null ? null : (
        <RenameMappingDialog
          mapping={renaming}
          onClose={() => {
            setRenaming(null);
          }}
        />
      )}
      {deleting === null ? null : (
        <DeleteMappingDialog
          mapping={deleting}
          onClose={() => {
            setDeleting(null);
          }}
        />
      )}
    </Card>
  );
}

export function LearnedPage() {
  const canWrite = useCanWrite();
  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">What PerFin has learned</h1>
        <Link to="/documents">Back to documents</Link>
      </div>
      <Card title="How files are recognised">
        <ul>
          <li>
            <strong>Accounts</strong> by the last four digits printed on a statement (kept on the account; edit them under{" "}
            <Link to="/accounts">Accounts</Link>) and, when two accounts share them, by the institution.
          </li>
          <li>
            <strong>Institutions</strong> by the name in a statement&rsquo;s header, matched against the institutions of your
            accounts.
          </li>
          <li>
            <strong>Spreadsheets</strong> (CSV, TSV, XLSX) by their header row, through a saved column mapping.
          </li>
          <li>
            <strong>Statement layouts</strong> the ordinary readers get wrong, through what you taught on a page.
          </li>
          <li>
            <strong>Categories</strong> through <Link to="/rules">rules</Link>.
          </li>
        </ul>
        <p className="ledger-muted">Everything here is yours to remove. Nothing below is shared outside this household.</p>
      </Card>
      <LayoutsCard canWrite={canWrite} />
      <MappingsCard canWrite={canWrite} />
    </section>
  );
}
