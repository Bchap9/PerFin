// SPDX-License-Identifier: AGPL-3.0-or-later
import "./tax.css";
/**
 * One tax year: status, reminder, the document checklist (expected, received,
 * not applicable) with the amounts read from each linked form, the deductible
 * roll-up by category and schedule hint, and the accountant export.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { queryRoots } from "../../lib/queryKeys";
import { useAuthSession } from "../auth/session";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { useRouteParam } from "../planning/useRouteParam";
import {
  addChecklistItem,
  deleteChecklistItem,
  getChecklist,
  getSummary,
  getTaxYear,
  learnChecklist,
  listForms,
  reminderText,
  STATUS_LABEL,
  STATUS_TONE,
  taxKeys,
  updateChecklistItem,
  updateTaxYear,
  US_FORMS,
  YEAR_STATE_LABEL,
  type ChecklistItem,
  type ChecklistStatus,
  type FormData,
  type TaxYear,
  type TaxYearState,
} from "./api";
import { ExportCard } from "./ExportCard";

const STATUS_OPTIONS = Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label }));

function AddItemDialog({ year, onClose }: { year: number; onClose: () => void }) {
  const [form, setForm] = useState("");
  const [issuer, setIssuer] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const add = useAction((body: { expected_form: string; issuer: string | null }) => addChecklistItem(year, body), {
    invalidate: [queryRoots.tax],
    success: "Document added to the checklist",
  });
  const submit = async (): Promise<void> => {
    if (form === "") {
      setProblem("Choose a form.");
      return;
    }
    setProblem(null);
    const done = await add.run({ expected_form: form, issuer: issuer.trim() === "" ? null : issuer.trim() });
    if (done !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title="Add an expected document"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={add.busy} onClick={() => void submit()}>
            Add to checklist
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Form" error={problem ?? undefined} required>
          <Select
            placeholder="Choose a form"
            options={US_FORMS.map((value) => ({ value, label: value }))}
            value={form}
            onChange={(event) => {
              setForm(event.target.value);
            }}
          />
        </Field>
        <Field label="Issuer" hint="Optional. The employer, bank or lender that sends it.">
          <Input
            value={issuer}
            onChange={(event) => {
              setIssuer(event.target.value);
            }}
          />
        </Field>
        <ErrorLine message={add.error} />
      </div>
    </Dialog>
  );
}

function BoxList({ forms }: { forms: readonly FormData[] }) {
  if (forms.length === 0) {
    return <span className="muted">No amounts read</span>;
  }
  return (
    <>
      {forms.map((form) => (
        <dl key={form.id} className="dashboard-figures" aria-label={`Amounts on ${form.form_type}`}>
          {form.boxes.map((box) => (
            <div key={box.box}>
              <dt>{box.label}</dt>
              <dd>{box.amount}</dd>
            </div>
          ))}
        </dl>
      ))}
    </>
  );
}

function ChecklistCard({ year, locked, canWrite }: { year: number; locked: boolean; canWrite: boolean }) {
  const checklist = useQuery({ queryKey: taxKeys.checklist(year), queryFn: () => getChecklist(year) });
  const forms = useQuery({ queryKey: taxKeys.forms(year), queryFn: () => listForms(year) });
  const [adding, setAdding] = useState(false);
  const invalidate = [queryRoots.tax];
  const change = useAction((id: string, status: ChecklistStatus) => updateChecklistItem(year, id, { status }), { invalidate });
  const remove = useAction((id: string) => deleteChecklistItem(year, id), { invalidate, success: "Removed from the checklist" });
  const learn = useAction(() => learnChecklist(year), {
    invalidate,
    success: (result) => (result.added === 0 ? "Nothing new to add" : `Added ${String(result.added)} from last year`),
  });
  const editable = canWrite && !locked;
  const formsByDocument = new Map<string, FormData[]>();
  for (const form of forms.data ?? []) {
    formsByDocument.set(form.document_id, [...(formsByDocument.get(form.document_id) ?? []), form]);
  }
  const columns: TableColumn<ChecklistItem>[] = [
    {
      id: "form",
      header: "Form",
      rowHeader: true,
      cell: (row) => (row.form_title === null ? row.expected_form : `${row.expected_form} ${row.form_title}`),
    },
    { id: "issuer", header: "Issuer", cell: (row) => row.issuer ?? "" },
    {
      id: "status",
      header: "Status",
      cell: (row) =>
        editable ? (
          <Select
            className="tax-status-select"
            aria-label={`Status of ${row.expected_form}${row.issuer === null ? "" : ` from ${row.issuer}`}`}
            options={STATUS_OPTIONS}
            value={row.status}
            onChange={(event) => {
              void change.run(row.id, event.target.value as ChecklistStatus);
            }}
          />
        ) : (
          <Badge tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</Badge>
        ),
    },
    {
      id: "document",
      header: "Document and amounts",
      cell: (row) =>
        row.document_id === null ? (
          <span className="muted">{row.status === "received" ? "Document not visible to you" : "Not received"}</span>
        ) : (
          <>
            <Link to="/documents/$documentId" params={{ documentId: row.document_id }}>
              Open document
            </Link>
            <BoxList forms={formsByDocument.get(row.document_id) ?? []} />
          </>
        ),
    },
    {
      id: "remove",
      header: <span className="visually-hidden">Remove</span>,
      align: "end",
      cell: (row) =>
        editable ? (
          <Button
            size="sm"
            variant="danger"
            aria-label={`Remove ${row.expected_form}${row.issuer === null ? "" : ` from ${row.issuer}`} from the checklist`}
            onClick={() => void remove.run(row.id)}
          >
            Remove
          </Button>
        ) : null,
    },
  ];
  return (
    <Card
      title="Checklist"
      headingLevel={2}
      actions={
        editable ? (
          <>
            <Button size="sm" loading={learn.busy} onClick={() => void learn.run()}>
              Learn from last year
            </Button>
            <Button
              size="sm"
              variant="primary"
              onClick={() => {
                setAdding(true);
              }}
            >
              Add document
            </Button>
          </>
        ) : undefined
      }
    >
      <QueryState loading={checklist.isPending} error={checklist.error} label="the checklist">
        <Table
          caption={`Documents expected for ${String(year)}`}
          captionHidden
          columns={columns}
          rows={checklist.data?.items ?? []}
          rowKey={(row) => row.id}
          empty={<EmptyState title="Nothing expected yet" description="Add the forms you expect, or learn them from last year." />}
        />
        <ErrorLine message={change.error ?? remove.error ?? learn.error} />
      </QueryState>
      {adding ? (
        <AddItemDialog
          year={year}
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
    </Card>
  );
}

function SummaryCard({ year }: { year: number }) {
  const query = useQuery({ queryKey: taxKeys.summary(year), queryFn: () => getSummary(year) });
  const summary = query.data;
  return (
    <Card title="Deductible roll-up" headingLevel={2}>
      <QueryState loading={query.isPending} error={query.error} label="the roll-up">
        {summary === undefined ? null : (
          <div className="planning-form">
            {summary.partial ? (
              <p role="note">Totals cover only the accounts you can see.</p>
            ) : null}
            <dl className="dashboard-figures">
              {Object.entries(summary.by_treatment).map(([treatment, money]) => (
                <div key={treatment}>
                  <dt>{treatment.replaceAll("_", " ")}</dt>
                  <dd>
                    <MoneyText amount={money.amount} currency={money.currency} tone="none" />
                  </dd>
                </div>
              ))}
            </dl>
            <Table
              caption="Totals by category"
              columns={[
                { id: "category", header: "Category", rowHeader: true, cell: (row) => row.category_name ?? "Flagged splits" },
                { id: "hint", header: "Schedule", cell: (row) => [row.schedule, row.line].filter((part) => part !== null).join(" ") },
                {
                  id: "total",
                  header: "Total",
                  align: "end",
                  cell: (row) => <MoneyText amount={row.total.amount} currency={row.total.currency} tone="none" />,
                },
              ]}
              rows={summary.categories}
              rowKey={(row) => `${row.category_id ?? "none"}:${row.schedule ?? ""}`}
              empty={<EmptyState title="Nothing tagged" description="Give categories a tax treatment, or flag splits, to see them here." />}
            />
            {summary.by_hint.length === 0 ? null : (
              <Table
                caption="Totals by schedule"
                columns={[
                  {
                    id: "hint",
                    header: "Schedule and line",
                    rowHeader: true,
                    cell: (row) => [row.schedule ?? "No schedule", row.line].filter((part) => part !== null).join(" "),
                  },
                  {
                    id: "total",
                    header: "Total",
                    align: "end",
                    cell: (row) => <MoneyText amount={row.total.amount} currency={row.total.currency} tone="none" />,
                  },
                ]}
                rows={summary.by_hint}
                rowKey={(row) => `${row.schedule ?? ""}:${row.line ?? ""}`}
              />
            )}
            {summary.unconverted.length === 0 ? null : (
              <p role="note">Some amounts have no exchange rate and are left out of these totals.</p>
            )}
            <p>
              <Link to="/tax/mapping">Edit schedule hints</Link>. Hints are for your accountant, not tax advice.
            </p>
          </div>
        )}
      </QueryState>
    </Card>
  );
}

function YearActions({ year, canWrite, isAdmin }: { year: TaxYear; canWrite: boolean; isAdmin: boolean }) {
  const move = useAction((state: TaxYearState) => updateTaxYear(year.year, { state }), {
    invalidate: [queryRoots.tax],
    success: "Tax year updated",
  });
  if (!canWrite) {
    return null;
  }
  const targets: { state: TaxYearState; label: string }[] =
    year.state === "open"
      ? [
          { state: "filed", label: "Mark as filed" },
          { state: "closed", label: "Close year" },
        ]
      : year.state === "filed"
        ? [
            { state: "open", label: "Reopen" },
            { state: "closed", label: "Close year" },
          ]
        : isAdmin
          ? [{ state: "open", label: "Reopen" }]
          : [];
  return (
    <>
      {targets.map((target) => (
        <Button key={target.state} loading={move.busy} onClick={() => void move.run(target.state)}>
          {target.label}
        </Button>
      ))}
    </>
  );
}

export function TaxYearPage() {
  const param = useRouteParam("year");
  const year = Number(param);
  const { session } = useAuthSession();
  const role = session?.household?.role ?? null;
  const canWrite = role === "owner" || role === "admin" || role === "member";
  const isAdmin = role === "owner" || role === "admin";
  const query = useQuery({ queryKey: taxKeys.year(year), queryFn: () => getTaxYear(year), enabled: Number.isInteger(year) });
  const data = query.data;
  const reminder = data?.state === "open" ? reminderText(data.checklist.expected) : null;
  return (
    <PlanningPage
      title={`Tax year ${param}`}
      actions={
        <>
          <Link to="/tax">All tax years</Link>
          {data === undefined ? null : <YearActions year={data} canWrite={canWrite} isAdmin={isAdmin} />}
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="the tax year">
        {data === undefined ? null : (
          <>
            <p>
              <Badge tone={data.state === "open" ? "info" : "neutral"}>{YEAR_STATE_LABEL[data.state]}</Badge> {data.jurisdiction}
              {data.filing_status === null ? "" : `, ${data.filing_status.replaceAll("_", " ")}`}.{" "}
              {String(data.checklist.received)} received, {String(data.checklist.expected)} expected,{" "}
              {String(data.checklist.not_applicable)} not applicable.
            </p>
            {reminder === null ? null : (
              <p role="status">
                <Badge tone="warning">Reminder</Badge> {reminder}.
              </p>
            )}
            <ChecklistCard year={year} locked={data.state === "closed"} canWrite={canWrite} />
            <SummaryCard year={year} />
            <ExportCard year={year} canExport={isAdmin} />
          </>
        )}
      </QueryState>
    </PlanningPage>
  );
}
