// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import { formatFraction, labelOf } from "../investments/format";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts, useCategories, usePayees } from "../planning/queries";
import { useAction } from "../planning/useAction";
import {
  CADENCE_LABELS,
  CADENCES,
  createSchedule,
  deleteSchedule,
  detectSchedules,
  getCandidates,
  listSchedules,
  recurringKeys,
  updateSchedule,
  type Cadence,
  type Candidate,
  type Schedule,
} from "./api";
import { buildScheduleBody, cadenceText, emptyScheduleForm, isBill, type ScheduleForm, type ScheduleProblems } from "./scheduleForm";

function ScheduleDialog({ onClose }: { onClose: () => void }) {
  const accounts = useAccounts();
  const payees = usePayees();
  const categories = useCategories();
  const editable = (accounts.data ?? []).filter((a) => a.can_edit && !a.is_closed);
  const [form, setForm] = useState<ScheduleForm>(emptyScheduleForm(todayIsoDate()));
  const [problems, setProblems] = useState<ScheduleProblems>({});
  const save = useAction(createSchedule, { invalidate: [queryRoots.recurring], success: "Recurring item added" });
  const accountId = form.accountId === "" ? (editable[0]?.id ?? "") : form.accountId;
  const currency = editable.find((a) => a.id === accountId)?.currency ?? form.currency;
  const set = <K extends keyof ScheduleForm>(key: K, value: ScheduleForm[K]): void => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const submit = async (): Promise<void> => {
    const result = buildScheduleBody({ ...form, accountId, currency });
    if (!result.ok) {
      setProblems(result.problems);
      return;
    }
    setProblems({});
    const done = await save.run(result.body);
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="New recurring item"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Add item
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Name" error={problems.name} required>
          <Input value={form.name} onChange={(event) => { set("name", event.target.value); }} />
        </Field>
        <Field label="Account" error={problems.account} required>
          <Select value={accountId} onChange={(event) => { set("accountId", event.target.value); }} options={editable.map((a) => ({ value: a.id, label: `${a.nickname ?? a.name} (${a.currency})` }))} />
        </Field>
        <Field label="Kind">
          <Select value={form.direction} onChange={(event) => { set("direction", event.target.value === "income" ? "income" : "bill"); }} options={[{ value: "bill", label: "Bill (money out)" }, { value: "income", label: "Income (money in)" }]} />
        </Field>
        <div className="planning-row">
          <Field label={`Lowest amount (${currency})`} error={problems.amount}>
            <Input inputMode="decimal" value={form.low} onChange={(event) => { set("low", event.target.value); }} />
          </Field>
          <Field label="Highest amount" hint="Blank: same as lowest">
            <Input inputMode="decimal" value={form.high} onChange={(event) => { set("high", event.target.value); }} />
          </Field>
        </div>
        <div className="planning-row">
          <Field label="Repeats every" error={problems.interval}>
            <Input inputMode="numeric" value={form.intervalCount} onChange={(event) => { set("intervalCount", event.target.value); }} />
          </Field>
          <Field label="Period">
            <Select value={form.cadence} onChange={(event) => { set("cadence", event.target.value as Cadence); }} options={CADENCES.map((c) => ({ value: c, label: labelOf(CADENCE_LABELS, c) }))} />
          </Field>
        </div>
        <Field label="Next due" error={problems.nextDue}>
          <Input type="date" value={form.nextDue} onChange={(event) => { set("nextDue", event.target.value); }} />
        </Field>
        <Field label="Payee">
          <Select value={form.payeeId} placeholder="None" onChange={(event) => { set("payeeId", event.target.value); }} options={(payees.data ?? []).map((p) => ({ value: p.id, label: p.name }))} />
        </Field>
        <Field label="Category">
          <Select value={form.categoryId} placeholder="None" onChange={(event) => { set("categoryId", event.target.value); }} options={(categories.data ?? []).map((c) => ({ value: c.id, label: c.name }))} />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

function amountRange(min: { amount: string; currency: string }, max: { amount: string; currency: string }) {
  return min.amount === max.amount ? (
    <MoneyText amount={min.amount} currency={min.currency} />
  ) : (
    <>
      <MoneyText amount={min.amount} currency={min.currency} /> to <MoneyText amount={max.amount} currency={max.currency} />
    </>
  );
}

export function RecurringTab() {
  const query = useQuery({ queryKey: recurringKeys.list, queryFn: listSchedules });
  const candidates = useQuery({ queryKey: recurringKeys.candidates, queryFn: getCandidates });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const [adding, setAdding] = useState(false);
  const invalidate = [queryRoots.recurring];
  const toggle = useAction((schedule: Schedule) => updateSchedule(schedule.id, { active: !schedule.active }), { invalidate });
  const remove = useAction(deleteSchedule, { invalidate, success: "Recurring item deleted" });
  const detect = useAction(detectSchedules, {
    invalidate,
    success: (result) => `Found ${String(result.created)} new, updated ${String(result.updated)}`,
  });

  const columns: TableColumn<Schedule>[] = [
    { id: "name", header: "Name", rowHeader: true, cell: (s) => s.name },
    { id: "account", header: "Account", cell: (s) => accountName(s.account_id) },
    { id: "kind", header: "Kind", cell: (s) => (isBill(s) ? "Bill" : "Income") },
    { id: "cadence", header: "Repeats", cell: (s) => cadenceText(s.cadence, s.interval_count) },
    { id: "amount", header: "Amount", align: "end", cell: (s) => amountRange(s.amount_min, s.amount_max) },
    { id: "next", header: "Next due", cell: (s) => formatDate(s.next_due) ?? s.next_due },
    { id: "origin", header: "Source", cell: (s) => <Badge tone={s.origin === "detected" ? "info" : "neutral"}>{s.origin === "detected" ? "Found for you" : "Added by you"}</Badge> },
    { id: "state", header: "State", cell: (s) => (s.active ? <Badge tone="success">Active</Badge> : <Badge>Paused</Badge>) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (s) => (
        <span className="planning-actions">
          <Button size="sm" aria-label={`${s.active ? "Pause" : "Resume"} ${s.name}`} onClick={() => void toggle.run(s)}>
            {s.active ? "Pause" : "Resume"}
          </Button>
          <Button size="sm" variant="danger" aria-label={`Delete ${s.name}`} onClick={() => void remove.run(s.id)}>
            Delete
          </Button>
        </span>
      ),
    },
  ];

  const suggestions = (candidates.data?.items ?? []).filter((item) => item.schedule_id === null);
  const suggestionColumns: TableColumn<Candidate>[] = [
    { id: "name", header: "Looks like", rowHeader: true, cell: (c) => c.name },
    { id: "pattern", header: "Pattern", cell: (c) => c.pattern },
    { id: "amount", header: "Amount", align: "end", cell: (c) => amountRange(c.amount_min, c.amount_max) },
    { id: "next", header: "Next due", cell: (c) => formatDate(c.next_due) ?? c.next_due },
    { id: "seen", header: "Seen", align: "end", cell: (c) => `${String(c.occurrences)} times` },
    { id: "confidence", header: "Confidence", align: "end", cell: (c) => formatFraction(c.confidence, 0) },
  ];

  return (
    <div className="planning-stack">
      <div className="planning-actions">
        <Button variant="primary" onClick={() => { setAdding(true); }}>
          New recurring item
        </Button>
        <Button loading={detect.busy} onClick={() => void detect.run()}>
          Find recurring items
        </Button>
      </div>
      <ErrorLine message={detect.error ?? toggle.error ?? remove.error} />
      <QueryState loading={query.isPending} error={query.error} label="recurring items">
        <Table caption="Recurring items" columns={columns} rows={query.data ?? []} rowKey={(s) => s.id} maxHeight="none" empty="No recurring items yet." />
      </QueryState>
      {suggestions.length === 0 ? null : (
        <Card title="Suggestions" headingLevel={2}>
          <p className="muted">Patterns found in your transactions that are not saved yet. Choose Find recurring items to save them.</p>
          <Table caption="Suggested recurring items" captionHidden columns={suggestionColumns} rows={suggestions} rowKey={(c) => `${c.account_id}:${c.name}:${c.pattern}:${c.amount_min.amount}`} maxHeight="md" />
        </Card>
      )}
      {adding ? <ScheduleDialog onClose={() => { setAdding(false); }} /> : null}
    </div>
  );
}
