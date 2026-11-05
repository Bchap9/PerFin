// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { simulateLoan, type Loan, type Simulation } from "./api";
import { ScheduleTable } from "./ScheduleTable";
import { buildSimulateBody, emptySimulatorForm, MAX_ONE_OFF, type SimulatorForm, type SimulatorProblems } from "./simulatorForm";

let rowCounter = 0;
const nextKey = (): string => {
  rowCounter += 1;
  return `row-${String(rowCounter)}`;
};

const dateText = (iso: string | null): string => (iso === null ? "No payoff" : (formatDate(iso) ?? iso));

function Result({ result }: { result: Simulation }) {
  const { baseline, with_extras: withExtras } = result;
  return (
    <Card title="Result" headingLevel={3}>
      <div className="planning-form" role="status">
        <p>
          Interest saved:{" "}
          <strong>
            <MoneyText amount={result.interest_saved.amount} currency={result.interest_saved.currency} tone="none" />
          </strong>
          . Payments saved: <strong>{String(result.payments_saved)}</strong>.
        </p>
        <dl className="wealth-figures">
          <div>
            <dt>Payoff without extras</dt>
            <dd>{dateText(baseline.payoff_date)}</dd>
          </div>
          <div>
            <dt>Payoff with extras</dt>
            <dd>{dateText(withExtras.payoff_date)}</dd>
          </div>
          <div>
            <dt>Interest without extras</dt>
            <dd><MoneyText amount={baseline.total_interest.amount} currency={baseline.total_interest.currency} tone="none" /></dd>
          </div>
          <div>
            <dt>Interest with extras</dt>
            <dd><MoneyText amount={withExtras.total_interest.amount} currency={withExtras.total_interest.currency} tone="none" /></dd>
          </div>
          <div>
            <dt>Extra paid in total</dt>
            <dd><MoneyText amount={withExtras.total_extra.amount} currency={withExtras.total_extra.currency} tone="none" /></dd>
          </div>
        </dl>
        {result.schedule === undefined || result.schedule === null ? null : <ScheduleTable rows={result.schedule} caption="Schedule with extra payments" />}
      </div>
    </Card>
  );
}

/** What-if: extra principal payments, once or every month. Nothing is saved. */
export function SimulatorTab({ loan }: { loan: Loan }) {
  const currency = loan.principal_original.currency;
  const [form, setForm] = useState<SimulatorForm>(emptySimulatorForm());
  const [problems, setProblems] = useState<SimulatorProblems>({ rows: {} });
  const run = useAction((body: Parameters<typeof simulateLoan>[1]) => simulateLoan(loan.id, body));
  const [result, setResult] = useState<Simulation | null>(null);

  const submit = async (): Promise<void> => {
    const built = buildSimulateBody(form, currency);
    if (!built.ok) {
      setProblems(built.problems);
      return;
    }
    setProblems({ rows: {} });
    const done = await run.run(built.body);
    if (done !== undefined) {
      setResult(done.value);
    }
  };

  return (
    <div className="planning-stack">
      <form
        className="planning-form"
        aria-label="Extra payment simulator"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <fieldset className="planning-form">
          <legend>Every month</legend>
          <div className="planning-row">
            <Field label={`Extra per payment (${currency})`} error={problems.recurringAmount}>
              <Input inputMode="decimal" value={form.recurringAmount} onChange={(event) => { setForm({ ...form, recurringAmount: event.target.value }); }} />
            </Field>
            <Field label="From" error={problems.recurringStart} hint="Blank: today">
              <Input type="date" value={form.recurringStart} onChange={(event) => { setForm({ ...form, recurringStart: event.target.value }); }} />
            </Field>
            <Field label="Until" error={problems.recurringEnd} hint="Blank: payoff">
              <Input type="date" value={form.recurringEnd} onChange={(event) => { setForm({ ...form, recurringEnd: event.target.value }); }} />
            </Field>
          </div>
        </fieldset>
        <fieldset className="planning-form">
          <legend>One-off extra payments</legend>
          {form.oneOff.map((row, index) => (
            <div key={row.key} className="planning-row">
              <Field label={`Date ${String(index + 1)}`}>
                <Input
                  type="date"
                  value={row.on}
                  onChange={(event) => { setForm({ ...form, oneOff: form.oneOff.map((entry) => (entry.key === row.key ? { ...entry, on: event.target.value } : entry)) }); }}
                />
              </Field>
              <Field label={`Amount ${String(index + 1)} (${currency})`} error={problems.rows[row.key]}>
                <Input
                  inputMode="decimal"
                  value={row.amount}
                  onChange={(event) => { setForm({ ...form, oneOff: form.oneOff.map((entry) => (entry.key === row.key ? { ...entry, amount: event.target.value } : entry)) }); }}
                />
              </Field>
              <Button aria-label={`Remove one-off payment ${String(index + 1)}`} onClick={() => { setForm({ ...form, oneOff: form.oneOff.filter((entry) => entry.key !== row.key) }); }}>
                Remove
              </Button>
            </div>
          ))}
          <div>
            <Button
              disabled={form.oneOff.length >= MAX_ONE_OFF}
              onClick={() => { setForm({ ...form, oneOff: [...form.oneOff, { key: nextKey(), on: todayIsoDate(), amount: "" }] }); }}
            >
              Add a one-off payment
            </Button>
          </div>
        </fieldset>
        <Checkbox label="Show the full schedule with extras" checked={form.includeSchedule} onChange={(event) => { setForm({ ...form, includeSchedule: event.target.checked }); }} />
        <ErrorLine message={problems.form ?? run.error ?? null} />
        <div>
          <Button type="submit" variant="primary" loading={run.busy}>
            Simulate
          </Button>
        </div>
      </form>
      {result === null ? null : <Result result={result} />}
    </div>
  );
}
