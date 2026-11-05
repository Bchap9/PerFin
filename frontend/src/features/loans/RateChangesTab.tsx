// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { formatFraction, percentToFraction } from "../investments/format";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { addRateChange, deleteRateChange, listRateChanges, loanKeys, type Loan, type RateChange } from "./api";

export function RateChangesTab({ loan }: { loan: Loan }) {
  const query = useQuery({ queryKey: loanKeys.rateChanges(loan.id), queryFn: () => listRateChanges(loan.id) });
  const [effective, setEffective] = useState(todayIsoDate());
  const [rate, setRate] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.loans, queryRoots.netWorth];
  const add = useAction((body: Parameters<typeof addRateChange>[1]) => addRateChange(loan.id, body), { invalidate, success: "Rate change saved" });
  const remove = useAction((id: string) => deleteRateChange(loan.id, id), { invalidate, success: "Rate change deleted" });

  const columns: TableColumn<RateChange>[] = [
    { id: "date", header: "Effective", rowHeader: true, cell: (c) => formatDate(c.effective_date) ?? c.effective_date },
    { id: "rate", header: "New rate", align: "end", cell: (c) => formatFraction(c.new_rate, 3) },
    {
      id: "remove",
      header: <span className="visually-hidden">Delete</span>,
      align: "end",
      cell: (c) => (
        <Button size="sm" variant="danger" aria-label={`Delete the rate change of ${c.effective_date}`} onClick={() => void remove.run(c.id)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="planning-stack">
      {loan.rate_type === "fixed" ? <p className="muted">This loan has a fixed rate: its payment stays the same after a rate change.</p> : null}
      <Card title="Add a rate change" headingLevel={3}>
        <form
          className="planning-row"
          aria-label="Add a rate change"
          onSubmit={(event) => {
            event.preventDefault();
            const fraction = percentToFraction(rate);
            if (!isIsoDate(effective) || fraction === null) {
              setProblem("Enter a date and a yearly rate as a percent, e.g. 5.5.");
              return;
            }
            setProblem(null);
            void add.run({ effective_date: effective, new_rate: fraction }).then((done) => {
              if (done !== undefined) {
                setRate("");
              }
            });
          }}
        >
          <Field label="Effective date">
            <Input type="date" value={effective} onChange={(event) => { setEffective(event.target.value); }} />
          </Field>
          <Field label="New yearly rate (%)">
            <Input inputMode="decimal" value={rate} onChange={(event) => { setRate(event.target.value); }} />
          </Field>
          <Button type="submit" variant="primary" loading={add.busy}>
            Add rate change
          </Button>
        </form>
        <ErrorLine message={problem ?? add.error ?? remove.error} />
      </Card>
      <QueryState loading={query.isPending} error={query.error} label="rate changes">
        <Table caption="Rate changes" columns={columns} rows={query.data ?? []} rowKey={(c) => c.id} maxHeight="md" empty="No rate changes recorded." />
      </QueryState>
    </div>
  );
}
