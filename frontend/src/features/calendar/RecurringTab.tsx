// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { useCanWrite } from "../accounts/api";
import { labelOf } from "../investments/format";
import { ErrorLine, PlanForm, QueryState } from "../planning/PlanningPage";
import { ConfirmDelete } from "../planning/ConfirmDelete";
import { nameLookup, useAccounts, useCategories, usePayees } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { SUBSCRIPTION_FAMILIES } from "../subscriptions/api";
import { kindText, magnitude, statusText } from "../subscriptions/text";
import {
  CADENCE_LABELS,
  CADENCES,
  createSchedule,
  deleteSchedule,
  detectSchedules,
  listSchedules,
  recurringKeys,
  updateSchedule,
  type Cadence,
  type Schedule,
} from "./api";
import { buildScheduleBody, cadenceText, emptyScheduleForm, isBill, type ScheduleForm, type ScheduleProblems } from "./scheduleForm";

/** What the form changes on a series that exists: the fields a recurring item and a subscription share. */
export type EditableSchedule = Pick<Schedule, "id" | "name" | "account_id" | "cadence" | "interval_count" | "amount_min" | "amount_max">;

/** The stored, signed range as the positive lowest and highest a person types. */
function formFor(schedule: EditableSchedule, today: string): ScheduleForm {
  const bill = isBill(schedule);
  const low = magnitude(bill ? schedule.amount_max.amount : schedule.amount_min.amount);
  const high = magnitude(bill ? schedule.amount_min.amount : schedule.amount_max.amount);
  return {
    ...emptyScheduleForm(today),
    name: schedule.name,
    accountId: schedule.account_id,
    currency: schedule.amount_min.currency,
    direction: bill ? "bill" : "income",
    low,
    high: high === low ? "" : high,
    cadence: schedule.cadence,
    intervalCount: String(schedule.interval_count),
  };
}

/**
 * The schedule form. Without `editing` it adds a recurring item. With it, it
 * changes the name, the amount range and the cadence of that series and
 * nothing else: the account and the direction are fixed, and whether a series
 * is active is a decision of its own, never a side effect of an edit.
 */
export function ScheduleDialog({ onClose, editing }: { onClose: () => void; editing?: EditableSchedule }) {
  const accounts = useAccounts();
  const payees = usePayees();
  const categories = useCategories();
  const editable = (accounts.data ?? []).filter((a) => a.can_edit && !a.is_closed);
  const [form, setForm] = useState<ScheduleForm>(() => (editing === undefined ? emptyScheduleForm(todayIsoDate()) : formFor(editing, todayIsoDate())));
  const [problems, setProblems] = useState<ScheduleProblems>({});
  const create = useAction(createSchedule, { invalidate: SUBSCRIPTION_FAMILIES, success: "Recurring item added" });
  const update = useAction(updateSchedule, { invalidate: SUBSCRIPTION_FAMILIES, success: "Saved" });
  const save = editing === undefined ? create : update;
  const accountId = form.accountId === "" ? (editable[0]?.id ?? "") : form.accountId;
  const currency = editing === undefined ? (editable.find((a) => a.id === accountId)?.currency ?? form.currency) : form.currency;
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
    const { name, cadence, interval_count, amount_min, amount_max } = result.body;
    const done =
      editing === undefined ? await create.run(result.body) : await update.run(editing.id, { name, cadence, interval_count, amount_min, amount_max });
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={editing === undefined ? "New recurring item" : `Edit ${editing.name}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form="schedule-form" loading={save.busy}>
            {editing === undefined ? "Add item" : "Save"}
          </Button>
        </>
      }
    >
      <PlanForm id="schedule-form" submit={submit}>
        <Field label="Name" error={problems.name} required>
          <Input value={form.name} onChange={(event) => { set("name", event.target.value); }} />
        </Field>
        {editing === undefined ? (
          <>
            <Field label="Account" error={problems.account} required>
              <Select value={accountId} onChange={(event) => { set("accountId", event.target.value); }} options={editable.map((a) => ({ value: a.id, label: `${a.nickname ?? a.name} (${a.currency})` }))} />
            </Field>
            <Field label="Kind">
              <Select value={form.direction} onChange={(event) => { set("direction", event.target.value === "income" ? "income" : "bill"); }} options={[{ value: "bill", label: "Bill (money out)" }, { value: "income", label: "Income (money in)" }]} />
            </Field>
          </>
        ) : null}
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
        {editing === undefined ? (
          <>
            <Field label="Next due" error={problems.nextDue}>
              <Input type="date" value={form.nextDue} onChange={(event) => { set("nextDue", event.target.value); }} />
            </Field>
            <Field label="Payee">
              <Select value={form.payeeId} placeholder="None" onChange={(event) => { set("payeeId", event.target.value); }} options={(payees.data ?? []).map((p) => ({ value: p.id, label: p.name }))} />
            </Field>
            <Field label="Category">
              <Select value={form.categoryId} placeholder="None" onChange={(event) => { set("categoryId", event.target.value); }} options={(categories.data ?? []).map((c) => ({ value: c.id, label: c.name }))} />
            </Field>
          </>
        ) : null}
        <ErrorLine message={save.error} />
      </PlanForm>
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

/** What `active: true` means for a series that is not active, in the word for its state. */
const TURN_ON_TEXT = new Map<Schedule["status"], string>([
  ["suggested", "Confirm"],
  ["paused", "Resume"],
  ["ended", "Reopen"],
  ["dismissed", "Reopen"],
]);

function toggleText(schedule: Schedule): string {
  return schedule.active ? "Pause" : (TURN_ON_TEXT.get(schedule.status) ?? "Resume");
}

const STATE_TONE = new Map<Schedule["status"], "success" | "info" | "warning">([
  ["confirmed", "success"],
  ["active", "info"],
  ["suggested", "info"],
  ["paused", "warning"],
]);

/**
 * The recurring items. Adding, finding, pausing, deleting and dismissing show
 * only to who may write, and a row's buttons only when its account may be
 * changed: the server refuses the rest (Writer, ScheduleWriter).
 */
export function RecurringTab() {
  const query = useQuery({ queryKey: recurringKeys.list, queryFn: listSchedules });
  const canWrite = useCanWrite();
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const editableAccounts = new Set((accounts.data ?? []).filter((a) => a.can_edit).map((a) => a.id));
  const [adding, setAdding] = useState(false);
  // Pausing, confirming or dismissing here is the same decision the Subscriptions view shows.
  const invalidate = SUBSCRIPTION_FAMILIES;
  const toggle = useAction((schedule: Schedule) => updateSchedule(schedule.id, { active: !schedule.active }), { invalidate });
  const remove = useAction(deleteSchedule, { invalidate, success: "Recurring item deleted" });
  const [deleting, setDeleting] = useState<Schedule | null>(null);
  // A found item is not deleted: the row stays as "not recurring", so detection does not bring it back.
  const dismiss = useAction(deleteSchedule, { invalidate, success: "Marked as not recurring" });
  const detect = useAction(detectSchedules, {
    invalidate,
    success: (result) => `Found ${String(result.created)} new, updated ${String(result.updated)}`,
  });

  const actionsColumn: TableColumn<Schedule> = {
    id: "actions",
    header: <span className="visually-hidden">Actions</span>,
    align: "end",
    cell: (s) =>
      editableAccounts.has(s.account_id) ? (
        <span className="planning-actions">
          <Button size="sm" aria-label={`${toggleText(s)} ${s.name}`} onClick={() => void toggle.run(s)}>
            {toggleText(s)}
          </Button>
          {s.origin !== "detected" ? (
            <Button size="sm" variant="danger" aria-label={`Delete ${s.name}`} onClick={() => { setDeleting(s); }}>
              Delete
            </Button>
          ) : s.status === "dismissed" ? null : (
            <Button size="sm" aria-label={`Not recurring: ${s.name}`} onClick={() => void dismiss.run(s.id)}>
              Not recurring
            </Button>
          )}
        </span>
      ) : null,
  };

  const columns: TableColumn<Schedule>[] = [
    { id: "name", header: "Name", rowHeader: true, cell: (s) => s.name },
    { id: "account", header: "Account", cell: (s) => accountName(s.account_id) },
    { id: "kind", header: "Kind", cell: (s) => kindText(s.kind) },
    { id: "cadence", header: "Repeats", cell: (s) => cadenceText(s.cadence, s.interval_count) },
    { id: "amount", header: "Amount", align: "end", cell: (s) => amountRange(s.amount_min, s.amount_max) },
    { id: "next", header: "Next due", cell: (s) => formatDate(s.next_due) ?? s.next_due },
    { id: "origin", header: "Source", cell: (s) => (s.origin === "detected" ? "From your statements" : "Added by you") },
    { id: "state", header: "State", cell: (s) => <Badge tone={STATE_TONE.get(s.status) ?? "neutral"}>{statusText(s.status, s.kind)}</Badge> },
    ...(canWrite ? [actionsColumn] : []),
  ];

  return (
    <div className="planning-stack">
      {canWrite ? (
        <div className="planning-actions">
          <Button variant="primary" onClick={() => { setAdding(true); }}>
            New recurring item
          </Button>
          <Button loading={detect.busy} onClick={() => void detect.run()}>
            Find recurring items
          </Button>
        </div>
      ) : null}
      <p className="muted">
        What subscriptions and bills cost, what changed and what needs a look is under Subscriptions.{" "}
        <Link to="/plan/subscriptions">See subscriptions</Link>
      </p>
      <ErrorLine message={detect.error ?? toggle.error ?? remove.error ?? dismiss.error} />
      <QueryState loading={query.isPending} error={query.error} label="recurring items">
        <Table caption="Recurring items" columns={columns} rows={query.data ?? []} rowKey={(s) => s.id} maxHeight="none" empty="No recurring items yet." />
      </QueryState>
      {canWrite && adding ? <ScheduleDialog onClose={() => { setAdding(false); }} /> : null}
      {deleting === null ? null : (
        <ConfirmDelete
          title={`Delete ${deleting.name}?`}
          description="It leaves the calendar and the recurring items."
          label="Delete item"
          action={remove}
          confirm={() => remove.run(deleting.id)}
          onClose={() => { setDeleting(null); }}
        />
      )}
    </div>
  );
}
