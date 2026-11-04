// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate, startOfMonth } from "../../lib/dates";
import { useHouseholdCurrency } from "../planning/currency";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { budgetKeys, createBudget, listBudgets, type Budget, type BudgetCreate } from "./api";

type Period = Budget["period"];

const PERIODS: readonly { value: Period; label: string }[] = [
  { value: "monthly", label: "Monthly" },
  { value: "annual", label: "Annual (12 months)" },
  { value: "custom", label: "Custom range" },
];

function isPeriod(value: string): value is Period {
  return PERIODS.some((period) => period.value === value);
}

export function periodLabel(period: Period): string {
  return PERIODS.find((entry) => entry.value === period)?.label.split(" ")[0] ?? period;
}

function NewBudgetDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const baseCurrency = useHouseholdCurrency();
  const [name, setName] = useState("");
  const [period, setPeriod] = useState<Period>("monthly");
  const [start, setStart] = useState(startOfMonth(todayIsoDate()));
  const [end, setEnd] = useState("");
  const [rollover, setRollover] = useState(false);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const create = useAction(createBudget, { invalidate: [budgetKeys.list], success: "Budget created" });

  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    if (name.trim() === "") {
      found["name"] = "Enter a name.";
    }
    if (start === "") {
      found["start"] = "Choose a start date.";
    }
    if (period === "custom" && end === "") {
      found["end"] = "A custom budget needs an end date.";
    }
    setProblems(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    const body: BudgetCreate = {
      name: name.trim(),
      period,
      start_date: start,
      rollover,
      ...(period === "custom" ? { end_date: end } : {}),
      ...(baseCurrency === null ? {} : { currency: baseCurrency }),
    };
    const done = await create.run(body);
    if (done !== undefined) {
      onClose();
      await navigate({ to: "/budgets/$budgetId", params: { budgetId: done.value.id } });
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="New budget"
      description="Create the budget, then set an amount for each category."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={create.busy} onClick={() => void submit()}>
            Create budget
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Name" error={problems["name"]} required>
          <Input value={name} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Field label="Period">
          <Select
            value={period}
            options={PERIODS}
            onChange={(event) => {
              if (isPeriod(event.target.value)) {
                setPeriod(event.target.value);
              }
            }}
          />
        </Field>
        <Field label="Starts on" error={problems["start"]} required>
          <Input type="date" value={start} onChange={(event) => { setStart(event.target.value); }} />
        </Field>
        {period === "custom" ? (
          <Field label="Ends on" error={problems["end"]} required>
            <Input type="date" value={end} onChange={(event) => { setEnd(event.target.value); }} />
          </Field>
        ) : null}
        <Checkbox
          label="Roll unspent amounts into the next period"
          checked={rollover}
          onChange={(event) => { setRollover(event.target.checked); }}
        />
        <ErrorLine message={create.error} />
      </div>
    </Dialog>
  );
}

export function BudgetsPage() {
  const query = useQuery({ queryKey: budgetKeys.list, queryFn: listBudgets });
  const [creating, setCreating] = useState(false);
  const columns: TableColumn<Budget>[] = [
    {
      id: "name",
      header: "Budget",
      rowHeader: true,
      cell: (budget) => (
        <Link to="/budgets/$budgetId" params={{ budgetId: budget.id }}>
          {budget.name}
        </Link>
      ),
    },
    { id: "period", header: "Period", cell: (budget) => <Badge>{periodLabel(budget.period)}</Badge> },
    {
      id: "range",
      header: "Dates",
      cell: (budget) =>
        `${formatDate(budget.start_date) ?? budget.start_date}${budget.end_date === null ? "" : ` to ${formatDate(budget.end_date) ?? budget.end_date}`}`,
    },
    { id: "lines", header: "Categories", align: "end", cell: (budget) => String(budget.lines.length) },
    { id: "rollover", header: "Rollover", cell: (budget) => (budget.rollover ? "On" : "Off") },
  ];
  return (
    <PlanningPage
      title="Budgets"
      actions={
        <Button variant="primary" onClick={() => { setCreating(true); }}>
          New budget
        </Button>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="budgets">
        <Table
          caption="Budgets"
          captionHidden
          columns={columns}
          rows={query.data ?? []}
          rowKey={(budget) => budget.id}
          maxHeight="none"
          empty={<EmptyState title="No budgets yet" description="Create a budget to plan spending by category." headingLevel={3} />}
        />
      </QueryState>
      {creating ? <NewBudgetDialog onClose={() => { setCreating(false); }} /> : null}
    </PlanningPage>
  );
}
