// SPDX-License-Identifier: AGPL-3.0-or-later
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
import { Table, type TableColumn } from "../../components/Table";
import { todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { createTaxYear, listTaxYears, openMissing, reminderText, taxKeys, YEAR_STATE_LABEL, type TaxYear } from "./api";

function NewYearDialog({ onClose }: { onClose: () => void }) {
  const [year, setYear] = useState(String(Number(todayIsoDate().slice(0, 4)) - 1));
  const [problem, setProblem] = useState<string | null>(null);
  const create = useAction(createTaxYear, { invalidate: [queryRoots.tax], success: "Tax year opened" });
  const submit = async (): Promise<void> => {
    if (!/^\d{4}$/.test(year)) {
      setProblem("Enter a four-digit year.");
      return;
    }
    setProblem(null);
    const done = await create.run({ year: Number(year), jurisdiction: "US" });
    if (done !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title="Open a tax year"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={create.busy} onClick={() => void submit()}>
            Open year
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <p>The checklist starts from the forms you received the year before.</p>
        <Field label="Year" error={problem ?? undefined} required>
          <Input
            inputMode="numeric"
            value={year}
            onChange={(event) => {
              setYear(event.target.value);
            }}
          />
        </Field>
        <ErrorLine message={create.error} />
      </div>
    </Dialog>
  );
}

export function TaxYearsPage() {
  const query = useQuery({ queryKey: taxKeys.years, queryFn: listTaxYears });
  const [creating, setCreating] = useState(false);
  const years = query.data ?? [];
  const columns: TableColumn<TaxYear>[] = [
    {
      id: "year",
      header: "Year",
      rowHeader: true,
      cell: (row) => (
        <Link to="/tax/$year" params={{ year: String(row.year) }}>
          {String(row.year)}
        </Link>
      ),
    },
    {
      id: "state",
      header: "Status",
      cell: (row) => <Badge tone={row.state === "open" ? "info" : "neutral"}>{YEAR_STATE_LABEL[row.state]}</Badge>,
    },
    { id: "jurisdiction", header: "Jurisdiction", cell: (row) => row.jurisdiction },
    { id: "expected", header: "Expected", align: "end", cell: (row) => String(row.checklist.expected) },
    { id: "received", header: "Received", align: "end", cell: (row) => String(row.checklist.received) },
    { id: "na", header: "Not applicable", align: "end", cell: (row) => String(row.checklist.not_applicable) },
  ];
  return (
    <PlanningPage
      title="Tax"
      actions={
        <>
          <Link to="/tax/mapping" className="btn btn--secondary btn--md">
            Schedule hints
          </Link>
          <Button
            variant="primary"
            onClick={() => {
              setCreating(true);
            }}
          >
            Open tax year
          </Button>
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="tax years">
        {openMissing(years).map((entry) => (
          <p key={entry.year} role="status">
            <Badge tone="warning">{String(entry.year)}</Badge> {reminderText(entry.missing)}.
          </p>
        ))}
        <Card title="Tax years">
          <Table
            caption="Tax years"
            captionHidden
            columns={columns}
            rows={years}
            rowKey={(row) => row.id}
            empty={<EmptyState title="No tax years yet" description="Open a year, or upload a tax form and PerFin opens it for you." />}
          />
        </Card>
      </QueryState>
      {creating ? (
        <NewYearDialog
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
    </PlanningPage>
  );
}
