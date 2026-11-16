// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What PerFin has learned about this household's documents, to look at and to
 * take back: the layouts taught in the review queue ("Remember this layout",
 * "Teach PerFin"; `GET/DELETE /imports/layouts`) and the saved column
 * mappings (`/imports/mappings`), and the statement wording confirmed after
 * the local model suggested what it means (`GET/DELETE /imports/wording`:
 * phrases only, nothing of the statement). A layout or mapping learned from a wrong
 * answer would misread every later file of its kind, so each can be removed
 * here; the next file is then read the ordinary way and can be taught again.
 *
 * Above them, the same for categories: how the transactions got theirs
 * (`GET /categorisation/summary`, with "Check again" for owners and admins)
 * and what the household's own choices taught, merchant by merchant
 * (`GET/DELETE /categorisation/learned`). Forgetting an entry changes no
 * transaction; it only stops that memory from deciding the next one.
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
import { canManageMembers, useAuthSession } from "../auth/session";
import { forgetLearned, listLearned, refreshCategorisation, type LearnedCategory } from "../categories/api";
import { useAction } from "../planning/useAction";
import { JobProgress } from "../rules/JobProgress";
import { RULES_INTRO, SettingsPage } from "../settings/SettingsLayout";
import { txKeys, useCategories, useCategorisationSummary } from "../transactions/api";
import {
  deleteLayout,
  deleteMapping,
  deleteWording,
  importKeys,
  learnedKeys,
  listLayouts,
  listMappings,
  listWording,
  renameMapping,
  type LearnedLayout,
  type LearnedWording,
  type Mapping,
} from "./api";
import { kindLabel, layoutReads, mappingReads, openText, sourceCountLines, timesText, wordingMeans } from "./learned";

/** Under the transactions root: a change to the ledger can change what was learned. */
const learnedCategoriesKey = [...queryRoots.transactions, "learned-categories"] as const;

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
                  <Link to="/inbox/documents/$documentId" params={{ documentId: row.learned_from_document_id }}>
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

function ForgetWordingButton({ wording }: { wording: LearnedWording }) {
  const forget = useAction(() => deleteWording(wording.id), {
    invalidate: [queryRoots.documents],
    success: "Wording forgotten",
  });
  return (
    <>
      <Button
        size="sm"
        loading={forget.busy}
        aria-label={`Forget "${wording.phrase}"`}
        onClick={() => {
          void forget.run();
        }}
      >
        Forget
      </Button>
      {forget.error === null ? null : (
        <p role="alert" className="ledger-error">
          {forget.error}
        </p>
      )}
    </>
  );
}

function WordingCard({ canWrite }: { canWrite: boolean }) {
  const wording = useQuery({ queryKey: learnedKeys.wording, queryFn: listWording });
  return (
    <Card title="Statement wording">
      <p className="ledger-muted">
        Learned when you confirmed a kind of activity the local model suggested for wording PerFin did not know. Only the wording is
        kept: a section&rsquo;s heading, a broker&rsquo;s action words or a table&rsquo;s column heading, with every digit
        masked. No amount, date, security, account or name from the statement is stored, and the model itself keeps nothing.
      </p>
      {wording.isPending ? <Spinner label="Loading wording" /> : null}
      {wording.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(wording.error)}
        </p>
      ) : null}
      {wording.isSuccess ? (
        <Table<LearnedWording>
          caption="Learned statement wording"
          captionHidden
          rows={wording.data}
          rowKey={(row) => row.id}
          empty="Nothing learned yet. With the local model on, wording PerFin does not know is suggested a kind; confirming it teaches it."
          columns={[
            { id: "phrase", header: "Wording", rowHeader: true, cell: (row) => `"${row.phrase}"` },
            { id: "means", header: "Read as", cell: (row) => wordingMeans(row) },
            { id: "layout", header: "On statements of", cell: (row) => row.layout.replaceAll("-", " ").replaceAll("_", " ") },
            {
              id: "learned",
              header: "Learned",
              cell: (row) =>
                row.learned_from_document_id === null ? (
                  day(row.learned_at)
                ) : (
                  <Link to="/inbox/documents/$documentId" params={{ documentId: row.learned_from_document_id }}>
                    {day(row.learned_at)}
                  </Link>
                ),
            },
            ...(canWrite
              ? [
                  {
                    id: "actions",
                    header: <span className="visually-hidden">Actions</span>,
                    cell: (row: LearnedWording) => <ForgetWordingButton wording={row} />,
                  },
                ]
              : []),
          ]}
        />
      ) : null}
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

function HowCategorisedCard({ canRefresh }: { canRefresh: boolean }) {
  const summary = useCategorisationSummary();
  const [jobId, setJobId] = useState<string | null>(null);
  const refresh = useAction(refreshCategorisation);
  const data = summary.data;
  const lines = data === undefined ? [] : sourceCountLines(data.by_source);
  const open = data === undefined ? null : openText(data.uncategorised, data.suggestions);
  return (
    <Card
      title="How transactions got their category"
      actions={
        canRefresh ? (
          <Button
            size="sm"
            loading={refresh.busy}
            onClick={() => {
              void refresh.run().then((done) => {
                if (done !== undefined) {
                  setJobId(done.value.job_id);
                }
              });
            }}
          >
            Check again
          </Button>
        ) : undefined
      }
    >
      <p className="ledger-muted">
        Counted over the accounts you can see. A category you chose, one from the file, a rule or a payee is never changed by a
        guess. Checking again looks only at transactions nobody decided.
      </p>
      {summary.isPending ? <Spinner label="Loading the counts" /> : null}
      {summary.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(summary.error)}
        </p>
      ) : null}
      {data === undefined ? null : (
        <>
          {lines.length === 0 ? (
            <p>No transactions yet.</p>
          ) : (
            <ul aria-label="Transactions by how their category was decided">
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          {open === null ? null : <p>{open}</p>}
          <p>
            Merchant list version {data.dictionary_version}, with {data.dictionary_merchants}{" "}
            {data.dictionary_merchants === 1 ? "merchant" : "merchants"}.
          </p>
          {data.up_to_date ? null : (
            <p>
              <Badge tone="warning">Not up to date yet</Badge>{" "}
              <span className="ledger-muted">
                Some transactions were last looked at with an older merchant list. PerFin catches up by itself within the hour.
              </span>
            </p>
          )}
        </>
      )}
      {refresh.error === null ? null : (
        <p role="alert" className="ledger-error">
          {refresh.error}
        </p>
      )}
      {jobId === null ? null : <JobProgress jobId={jobId} />}
    </Card>
  );
}

function ForgetLearnedButton({ entry }: { entry: LearnedCategory }) {
  const forget = useAction(() => forgetLearned(entry.id), {
    invalidate: [learnedCategoriesKey, txKeys.summary],
    success: "Forgotten",
  });
  return (
    <>
      <Button
        size="sm"
        loading={forget.busy}
        aria-label={`Forget what was learned about ${entry.merchant}`}
        onClick={() => {
          void forget.run();
        }}
      >
        Forget
      </Button>
      {forget.error === null ? null : (
        <p role="alert" className="ledger-error">
          {forget.error}
        </p>
      )}
    </>
  );
}

function LearnedCategoriesCard({ canWrite }: { canWrite: boolean }) {
  const learned = useQuery({ queryKey: learnedCategoriesKey, queryFn: listLearned });
  const categories = useCategories();
  const categoryName = (id: string): string => {
    if (categories.data === undefined) {
      return "";
    }
    return categories.data.find((category) => category.id === id)?.name ?? "A category that no longer exists";
  };
  return (
    <Card title="Categories you taught">
      <p className="ledger-muted">
        Each time you choose a category for a transaction, PerFin counts it for that merchant and uses the most chosen one for
        the merchant&rsquo;s next transactions. Forgetting an entry changes no transaction.
      </p>
      {learned.isPending ? <Spinner label="Loading what was learned" /> : null}
      {learned.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(learned.error)}
        </p>
      ) : null}
      {learned.isSuccess ? (
        <Table<LearnedCategory>
          caption="Categories learned from you"
          captionHidden
          rows={learned.data}
          rowKey={(row) => row.id}
          empty="Nothing learned yet. Choose a category for a transaction and its merchant appears here."
          columns={[
            {
              id: "merchant",
              header: "Merchant",
              rowHeader: true,
              cell: (row) => row.merchant,
            },
            { id: "direction", header: "Money", cell: (row) => (row.direction === "in" ? "In" : "Out") },
            { id: "category", header: "Category", cell: (row) => categoryName(row.category_id) },
            { id: "times", header: "Chosen", cell: (row) => timesText(row.decisions, row.total) },
            { id: "last", header: "Last time", cell: (row) => day(row.last_decided_at) },
            {
              id: "actions",
              header: <span className="visually-hidden">Actions</span>,
              cell: (row: LearnedCategory) => (
                <span className="ledger-actions">
                  {row.rule_id === null ? null : (
                    <Link to="/settings/rules/$ruleId" params={{ ruleId: row.rule_id }} aria-label={`The rule for ${row.merchant}`}>
                      Has a rule
                    </Link>
                  )}
                  {canWrite && row.offer_rule && row.rule_id === null && row.example_transaction_id !== null ? (
                    <Link to="/transactions" search={{ always: row.example_transaction_id }} aria-label={`Make ${row.merchant} a rule`}>
                      Make it a rule
                    </Link>
                  ) : null}
                  {canWrite ? <ForgetLearnedButton entry={row} /> : null}
                </span>
              ),
            },
          ]}
        />
      ) : null}
    </Card>
  );
}

export function LearnedPage() {
  const canWrite = useCanWrite();
  const { session } = useAuthSession();
  // The server lets owners and admins start a refresh; forgetting an entry is a member's.
  const canRefresh = canManageMembers(session?.household?.role);
  return (
    <SettingsPage title="What PerFin has learned">
      <p>{RULES_INTRO}</p>
      <div className="ledger-stack">
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
            <strong>Statement wording</strong> the ordinary readers have no word for, through a kind the optional local model
            suggested and you confirmed.
          </li>
          <li>
            <strong>Categories</strong> through your <Link to="/settings/rules">rules</Link>, what PerFin learned from the categories you
            chose, and the merchant list that ships with PerFin.
          </li>
        </ul>
        <p className="ledger-muted">Everything here is yours to remove. Nothing below is shared outside this household.</p>
      </Card>
      <HowCategorisedCard canRefresh={canRefresh} />
      <LearnedCategoriesCard canWrite={canWrite} />
      <LayoutsCard canWrite={canWrite} />
      <WordingCard canWrite={canWrite} />
      <MappingsCard canWrite={canWrite} />
      </div>
    </SettingsPage>
  );
}
