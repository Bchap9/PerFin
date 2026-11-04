// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One budget: the period report (budgeted, carried over, actual, forecast,
 * remaining per category) with period stepping, and an editor for its lines.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { Badge, type BadgeTone } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { addMonths, formatDate, formatMonth } from "../../lib/dates";
import { isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { categoryOptions } from "../categories/tree";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useCategories } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { useRouteParam } from "../planning/useRouteParam";
import {
  budgetKeys,
  deleteBudget,
  getBudget,
  getBudgetReport,
  updateBudget,
  type Budget,
  type BudgetReport,
  type BudgetReportLine,
} from "./api";
import { barWidth, lineStatus, steppedPeriod, type LineStatus } from "./report";

const STATUS: Record<LineStatus, { tone: BadgeTone; label: string }> = {
  ok: { tone: "success", label: "On track" },
  "forecast-over": { tone: "warning", label: "Forecast over" },
  over: { tone: "danger", label: "Over budget" },
};

function periodTitle(report: BudgetReport, budget: Budget): string {
  if (budget.period === "monthly") {
    return formatMonth(report.period.start, { style: "long" }) ?? report.period.start;
  }
  const from = formatDate(report.period.start) ?? report.period.start;
  const to = formatDate(report.period.end) ?? report.period.end;
  return `${from} to ${to}`;
}

export function ReportTable({ report }: { report: BudgetReport }) {
  const currency = report.currency;
  const columns: TableColumn<BudgetReportLine>[] = [
    { id: "name", header: "Category", rowHeader: true, cell: (line) => line.name ?? "Unknown" },
    { id: "budgeted", header: "Budgeted", align: "end", cell: (line) => <MoneyText amount={line.budgeted.amount} currency={currency} tone="none" /> },
    { id: "carried", header: "Carried over", align: "end", cell: (line) => <MoneyText amount={line.carried_over.amount} currency={currency} tone="none" /> },
    { id: "actual", header: "Actual", align: "end", cell: (line) => <MoneyText amount={line.actual.amount} currency={currency} tone="none" /> },
    { id: "forecast", header: "Forecast", align: "end", cell: (line) => <MoneyText amount={line.forecast.amount} currency={currency} tone="none" /> },
    { id: "remaining", header: "Remaining", align: "end", cell: (line) => <MoneyText amount={line.remaining.amount} currency={currency} /> },
    {
      id: "progress",
      header: "Used",
      cell: (line) => {
        const width = barWidth(line.actual.amount, line.available.amount);
        const status = lineStatus(line, line.kind);
        return (
          <span className="bar" role="img" aria-label={`${String(Math.round(width))} percent used`}>
            <span className={status === "over" ? "bar__fill bar__fill--over" : "bar__fill"} style={{ width: `${String(width)}%` }} />
          </span>
        );
      },
    },
    {
      id: "status",
      header: "Status",
      cell: (line) => {
        const status = STATUS[lineStatus(line, line.kind)];
        return <Badge tone={status.tone}>{status.label}</Badge>;
      },
    },
  ];
  const total = report.expense_totals;
  return (
    <Table
      caption="Budget report by category"
      captionHidden
      columns={columns}
      rows={report.lines}
      rowKey={(line) => line.category_id}
      maxHeight="none"
      empty="This budget has no categories yet. Edit the budget to add some."
      footer={
        <>
          <tr>
            <th scope="row">Total spending</th>
            <td className="align-end"><MoneyText amount={total.budgeted.amount} currency={currency} tone="none" /></td>
            <td className="align-end"><MoneyText amount={total.carried_over.amount} currency={currency} tone="none" /></td>
            <td className="align-end"><MoneyText amount={total.actual.amount} currency={currency} tone="none" /></td>
            <td className="align-end"><MoneyText amount={total.forecast.amount} currency={currency} tone="none" /></td>
            <td className="align-end"><MoneyText amount={total.remaining.amount} currency={currency} /></td>
            <td colSpan={2} />
          </tr>
          <tr>
            <th scope="row">Spending outside the budget</th>
            <td colSpan={2} />
            <td className="align-end"><MoneyText amount={report.unbudgeted_total.amount} currency={currency} tone="none" /></td>
            <td colSpan={4} />
          </tr>
        </>
      }
    />
  );
}

interface LineDraft {
  key: number;
  categoryId: string;
  amount: string;
}

function EditBudgetDialog({ budget, onClose }: { budget: Budget; onClose: () => void }) {
  const categories = useCategories();
  const [name, setName] = useState(budget.name);
  const [rollover, setRollover] = useState(budget.rollover);
  const [lines, setLines] = useState<LineDraft[]>(
    budget.lines.map((line, index) => ({ key: index, categoryId: line.category_id, amount: line.amount.amount })),
  );
  const [nextKey, setNextKey] = useState(budget.lines.length);
  const [problem, setProblem] = useState<string | null>(null);
  const save = useAction((body: Parameters<typeof updateBudget>[1]) => updateBudget(budget.id, body), {
    invalidate: [queryRoots.budgets],
    success: "Budget saved",
  });
  const options = categoryOptions(categories.data ?? [], (category) => category.kind === "expense" || category.kind === "income");

  const submit = async (): Promise<void> => {
    if (name.trim() === "") {
      setProblem("Enter a name.");
      return;
    }
    const chosen = lines.filter((line) => line.categoryId !== "");
    if (new Set(chosen.map((line) => line.categoryId)).size !== chosen.length) {
      setProblem("Each category can appear once.");
      return;
    }
    if (chosen.some((line) => !isDecimalString(line.amount) || line.amount.startsWith("-"))) {
      setProblem("Amounts must be numbers of zero or more, such as 250 or 99.50.");
      return;
    }
    setProblem(null);
    const done = await save.run({
      name: name.trim(),
      rollover,
      lines: chosen.map((line) => ({
        category_id: line.categoryId,
        amount: { amount: line.amount, currency: budget.currency },
      })),
    });
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Edit budget"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save budget
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Name" required>
          <Input value={name} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Checkbox label="Roll unspent amounts into the next period" checked={rollover} onChange={(event) => { setRollover(event.target.checked); }} />
        <fieldset className="planning-form">
          <legend>Amounts per {budget.period === "annual" ? "year" : budget.period === "monthly" ? "month" : "period"} ({budget.currency})</legend>
          {lines.map((line, index) => (
            <div className="planning-row" key={line.key}>
              <Field label={`Category ${String(index + 1)}`}>
                <Select
                  value={line.categoryId}
                  placeholder="Choose a category"
                  options={options}
                  onChange={(event) => {
                    setLines(lines.map((entry) => (entry.key === line.key ? { ...entry, categoryId: event.target.value } : entry)));
                  }}
                />
              </Field>
              <Field label={`Amount ${String(index + 1)}`}>
                <Input
                  inputMode="decimal"
                  value={line.amount}
                  onChange={(event) => {
                    setLines(lines.map((entry) => (entry.key === line.key ? { ...entry, amount: event.target.value } : entry)));
                  }}
                />
              </Field>
              <Button
                aria-label={`Remove line ${String(index + 1)}`}
                onClick={() => {
                  setLines(lines.filter((entry) => entry.key !== line.key));
                }}
              >
                Remove
              </Button>
            </div>
          ))}
          <div>
            <Button
              onClick={() => {
                setLines([...lines, { key: nextKey, categoryId: "", amount: "0" }]);
                setNextKey(nextKey + 1);
              }}
            >
              Add category
            </Button>
          </div>
        </fieldset>
        {problem === null ? null : <ErrorLine message={problem} />}
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

export function BudgetDetailPage() {
  const budgetId = useRouteParam("budgetId");
  const navigate = useNavigate();
  const [period, setPeriod] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const budgetQuery = useQuery({ queryKey: budgetKeys.one(budgetId), queryFn: () => getBudget(budgetId) });
  const reportQuery = useQuery({
    queryKey: budgetKeys.report(budgetId, period, pending),
    queryFn: () => getBudgetReport(budgetId, period, pending),
    placeholderData: (previous) => previous,
  });
  const remove = useAction(() => deleteBudget(budgetId), { invalidate: [queryRoots.budgets], success: "Budget deleted" });
  const budget = budgetQuery.data;
  const report = reportQuery.data;
  const previous = budget !== undefined && report !== undefined ? steppedPeriod(report.period.start, budget.period, -1, addMonths) : null;
  const next = budget !== undefined && report !== undefined ? steppedPeriod(report.period.start, budget.period, 1, addMonths) : null;

  return (
    <PlanningPage
      title={budget?.name ?? "Budget"}
      actions={
        budget === undefined ? undefined : (
          <>
            <Button onClick={() => { setEditing(true); }}>Edit budget</Button>
            <Button variant="danger" onClick={() => { setConfirmDelete(true); }}>Delete</Button>
          </>
        )
      }
    >
      <p>
        <Link to="/budgets">All budgets</Link>
      </p>
      <QueryState loading={budgetQuery.isPending} error={budgetQuery.error} label="the budget">
        {budget === undefined ? null : (
          <>
            <div className="planning-row">
              {previous === null ? null : (
                <Button aria-label="Previous period" onClick={() => { setPeriod(previous); }}>
                  Previous
                </Button>
              )}
              <strong aria-live="polite">{report === undefined ? "" : periodTitle(report, budget)}</strong>
              {next === null ? null : (
                <Button aria-label="Next period" onClick={() => { setPeriod(next); }}>
                  Next
                </Button>
              )}
              <Checkbox label="Include transactions awaiting review" checked={pending} onChange={(event) => { setPending(event.target.checked); }} />
            </div>
            <QueryState loading={reportQuery.isPending} error={reportQuery.error} label="the report">
              {report === undefined ? null : <ReportTable report={report} />}
            </QueryState>
          </>
        )}
      </QueryState>
      {budget !== undefined && editing ? <EditBudgetDialog budget={budget} onClose={() => { setEditing(false); }} /> : null}
      {confirmDelete ? (
        <Dialog
          open
          onClose={() => { setConfirmDelete(false); }}
          title="Delete budget"
          description="Delete this budget? Transactions are not affected."
          footer={
            <>
              <Button onClick={() => { setConfirmDelete(false); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run().then(async (done) => {
                    if (done !== undefined) {
                      await navigate({ to: "/budgets" });
                    }
                  });
                }}
              >
                Delete budget
              </Button>
            </>
          }
        >
          <ErrorLine message={remove.error} />
        </Dialog>
      ) : null}
    </PlanningPage>
  );
}
