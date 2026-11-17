// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Plan > Goals: each goal with its progress bar, "On pace for <month year>"
 * when there is a projection, and its contributions. New goal, edit,
 * contribute, archive and delete show only to who may write; a viewer can
 * still read the contributions.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { ProgressBar } from "../../components/ProgressBar";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, formatMonth, todayIsoDate } from "../../lib/dates";
import { isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { useCanWrite } from "../accounts/api";
import { useHouseholdCurrency } from "../planning/currency";
import { ErrorLine, PlanForm, PlanningPage, QueryState } from "../planning/PlanningPage";
import { ConfirmDelete } from "../planning/ConfirmDelete";
import { useAccounts } from "../planning/queries";
import { useAction } from "../planning/useAction";
import {
  addContribution,
  createGoal,
  deleteGoal,
  goalKeys,
  listContributions,
  listGoals,
  removeContribution,
  updateGoal,
  type Contribution,
  type Goal,
} from "./api";
import { goalBarWidth, projectionText } from "./progress";

/**
 * "On pace for July 2027" while a goal is open, on track and has a projected
 * completion. A goal the server marks as off track reads "Projected for July
 * 2027" instead, so the line never contradicts its "Behind" badge. Otherwise null.
 */
export function onPaceText(goal: Goal): string | null {
  const projected = goal.progress.projected_completion;
  if (goal.achieved_at !== null || projected === null) {
    return null;
  }
  const month = formatMonth(projected, { style: "long" });
  if (month === null) {
    return null;
  }
  return goal.progress.on_track === false ? `Projected for ${month}` : `On pace for ${month}`;
}

/** The empty list's line: an invitation for who may add a goal, a plain statement for a viewer. */
function emptyDescription(canWrite: boolean, archived: boolean): string {
  if (canWrite) {
    return "Set a target and track what you have saved towards it.";
  }
  return archived ? "No goal in this household is archived." : "No one in this household has set a goal yet.";
}

function GoalDialog({ goal, onClose }: { goal: Goal | null; onClose: () => void }) {
  const accounts = useAccounts();
  const baseCurrency = useHouseholdCurrency();
  const currency = goal?.target.currency ?? baseCurrency ?? "USD";
  const [name, setName] = useState(goal?.name ?? "");
  const [target, setTarget] = useState(goal?.target.amount ?? "");
  const [targetDate, setTargetDate] = useState(goal?.target_date ?? "");
  const [accountIds, setAccountIds] = useState<string[]>(goal?.account_ids ?? []);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const save = useAction(
    (body: { name: string; target: { amount: string; currency: string }; target_date: string | null; account_ids: string[] }) =>
      goal === null ? createGoal(body) : updateGoal(goal.id, body),
    { invalidate: [queryRoots.goals, queryRoots.dashboard], success: goal === null ? "Goal added" : "Goal saved" },
  );

  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    if (name.trim() === "") {
      found["name"] = "Enter a name.";
    }
    if (!isDecimalString(target) || target.startsWith("-") || /^[0.]+$/.test(target)) {
      found["target"] = "Enter a target amount above zero.";
    }
    setProblems(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    const done = await save.run({
      name: name.trim(),
      target: { amount: target, currency },
      target_date: targetDate === "" ? null : targetDate,
      account_ids: accountIds,
    });
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={goal === null ? "New goal" : "Edit goal"}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" type="submit" form="goal-form" loading={save.busy}>
            Save goal
          </Button>
        </>
      }
    >
      <PlanForm id="goal-form" submit={submit}>
        <Field label="Name" error={problems["name"]} required>
          <Input value={name} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Field label={`Target amount (${currency})`} error={problems["target"]} required>
          <Input inputMode="decimal" value={target} onChange={(event) => { setTarget(event.target.value); }} />
        </Field>
        <Field label="Target date" hint="Optional. Used to tell whether you are on track.">
          <Input type="date" value={targetDate} onChange={(event) => { setTargetDate(event.target.value); }} />
        </Field>
        <fieldset className="planning-form">
          <legend>Accounts that count towards this goal</legend>
          {(accounts.data ?? []).length === 0 ? <p className="muted">No accounts available.</p> : null}
          {(accounts.data ?? []).map((account) => (
            <Checkbox
              key={account.id}
              label={account.nickname ?? account.name}
              checked={accountIds.includes(account.id)}
              onChange={(event) => {
                setAccountIds(event.target.checked ? [...accountIds, account.id] : accountIds.filter((id) => id !== account.id));
              }}
            />
          ))}
        </fieldset>
        <ErrorLine message={save.error} />
      </PlanForm>
    </Dialog>
  );
}

function ContributionsDialog({ goal, canWrite, onClose }: { goal: Goal; canWrite: boolean; onClose: () => void }) {
  const query = useQuery({ queryKey: goalKeys.contributions(goal.id), queryFn: () => listContributions(goal.id) });
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(todayIsoDate());
  const [memo, setMemo] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.goals, queryRoots.dashboard];
  const add = useAction((body: Parameters<typeof addContribution>[1]) => addContribution(goal.id, body), {
    invalidate,
    success: "Contribution added",
  });
  const remove = useAction((id: string) => removeContribution(goal.id, id), { invalidate, success: "Contribution removed" });
  const [removing, setRemoving] = useState<Contribution | null>(null);
  const columns: TableColumn<Contribution>[] = [
    { id: "date", header: "Date", rowHeader: true, cell: (row) => formatDate(row.contributed_on) ?? row.contributed_on },
    { id: "amount", header: "Amount", align: "end", cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="none" /> },
    { id: "memo", header: "Memo", cell: (row) => row.memo ?? "" },
  ];
  const removeColumn: TableColumn<Contribution> = {
    id: "remove",
    header: <span className="visually-hidden">Remove</span>,
    align: "end",
    cell: (row) => (
      <Button size="sm" variant="danger" aria-label={`Remove contribution of ${row.amount.amount} on ${row.contributed_on}`} onClick={() => { setRemoving(row); }}>
        Remove
      </Button>
    ),
  };
  return (
    <Dialog open onClose={onClose} size="lg" title={`Contributions to ${goal.name}`} footer={<Button onClick={onClose}>Close</Button>}>
      <div className="planning-form">
        {canWrite ? (
          <form
            className="planning-row"
            aria-label="Add a contribution"
            onSubmit={(event) => {
              event.preventDefault();
              if (!isDecimalString(amount) || amount.startsWith("-")) {
                setProblem("Enter an amount of zero or more.");
                return;
              }
              setProblem(null);
              void add
                .run({ amount: { amount, currency: goal.target.currency }, contributed_on: date, ...(memo.trim() === "" ? {} : { memo: memo.trim() }) })
                .then((done) => {
                  if (done !== undefined) {
                    setAmount("");
                    setMemo("");
                  }
                });
            }}
          >
            <Field label={`Amount (${goal.target.currency})`}>
              <Input inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); }} />
            </Field>
            <Field label="Date">
              <Input type="date" value={date} onChange={(event) => { setDate(event.target.value); }} />
            </Field>
            <Field label="Memo">
              <Input value={memo} onChange={(event) => { setMemo(event.target.value); }} />
            </Field>
            <Button type="submit" variant="primary" loading={add.busy}>
              Add contribution
            </Button>
          </form>
        ) : null}
        {problem === null ? null : <ErrorLine message={problem} />}
        <ErrorLine message={add.error ?? remove.error} />
        <QueryState loading={query.isPending} error={query.error} label="contributions">
          <Table
            caption="Contributions"
            captionHidden
            columns={canWrite ? [...columns, removeColumn] : columns}
            rows={query.data ?? []}
            rowKey={(row) => row.id}
            maxHeight="md"
            empty="No contributions recorded."
          />
        </QueryState>
      </div>
      {removing === null ? null : (
        <ConfirmDelete
          title="Remove this contribution?"
          description={`${removing.amount.amount} on ${formatDate(removing.contributed_on) ?? removing.contributed_on} no longer counts towards ${goal.name}.`}
          label="Remove contribution"
          action={remove}
          confirm={() => remove.run(removing.id)}
          onClose={() => { setRemoving(null); }}
        />
      )}
    </Dialog>
  );
}

function GoalCard({ goal, canWrite, onEdit, onContribute }: { goal: Goal; canWrite: boolean; onEdit: () => void; onContribute: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const invalidate = [queryRoots.goals, queryRoots.dashboard];
  const archive = useAction(() => updateGoal(goal.id, { archived: !goal.archived }), { invalidate, success: goal.archived ? "Goal restored" : "Goal archived" });
  const remove = useAction(() => deleteGoal(goal.id), { invalidate, success: "Goal deleted" });
  const width = goalBarWidth(goal);
  const { progress } = goal;
  const onPace = onPaceText(goal);
  return (
    <Card
      title={goal.name}
      headingLevel={3}
      actions={
        goal.achieved_at !== null ? (
          <Badge tone="success">Reached</Badge>
        ) : progress.on_track === null ? null : (
          <Badge tone={progress.on_track ? "success" : "warning"}>{progress.on_track ? "On track" : "Behind"}</Badge>
        )
      }
    >
      <div className="planning-form">
        <p>
          <MoneyText amount={progress.contributed.amount} currency={progress.contributed.currency} tone="none" /> of{" "}
          <MoneyText amount={goal.target.amount} currency={goal.target.currency} tone="none" />
          {" "}({progress.percent}%)
        </p>
        <ProgressBar value={width} max={100} label={`${goal.name}: ${String(Math.round(width))} percent complete`} />
        {onPace === null ? null : <p>{onPace}</p>}
        <p className="muted">
          Remaining <MoneyText amount={progress.remaining.amount} currency={progress.remaining.currency} tone="none" />
          {goal.target_date === null ? "" : `, target ${formatDate(goal.target_date) ?? goal.target_date}`}
          {progress.monthly_rate === null ? "" : ", saving "}
          {progress.monthly_rate === null ? null : <MoneyText amount={progress.monthly_rate.amount} currency={progress.monthly_rate.currency} tone="none" />}
          {progress.monthly_rate === null ? null : " a month"}
        </p>
        <p>{projectionText(goal)}</p>
        <div className="planning-actions">
          <Button size="sm" onClick={onContribute} aria-label={`Contributions to ${goal.name}`}>
            Contributions
          </Button>
          {canWrite ? (
            <>
              <Button size="sm" onClick={onEdit} aria-label={`Edit ${goal.name}`}>
                Edit
              </Button>
              <Button size="sm" loading={archive.busy} onClick={() => void archive.run()} aria-label={`${goal.archived ? "Restore" : "Archive"} ${goal.name}`}>
                {goal.archived ? "Restore" : "Archive"}
              </Button>
              <Button size="sm" variant="danger" onClick={() => { setConfirming(true); }} aria-label={`Delete ${goal.name}`}>
                Delete
              </Button>
            </>
          ) : null}
        </div>
        <ErrorLine message={archive.error} />
      </div>
      {canWrite && confirming ? (
        <ConfirmDelete
          title="Delete goal"
          description={`Delete "${goal.name}" and its contributions?`}
          label="Delete goal"
          action={remove}
          confirm={() => remove.run()}
          onClose={() => { setConfirming(false); }}
        />
      ) : null}
    </Card>
  );
}

export function GoalsPage() {
  const [archived, setArchived] = useState(false);
  const canWrite = useCanWrite();
  const query = useQuery({ queryKey: goalKeys.list(archived), queryFn: () => listGoals(archived) });
  const [editing, setEditing] = useState<Goal | "new" | null>(null);
  const [contributing, setContributing] = useState<Goal | null>(null);
  const goals = query.data ?? [];
  return (
    <PlanningPage
      title="Goals"
      actions={
        <>
          <Checkbox label="Show archived" checked={archived} onChange={(event) => { setArchived(event.target.checked); }} />
          {canWrite ? (
            <Button variant="primary" onClick={() => { setEditing("new"); }}>
              New goal
            </Button>
          ) : null}
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="goals">
        {goals.length === 0 ? (
          <EmptyState title={archived ? "No archived goals" : "No goals yet"} description={emptyDescription(canWrite, archived)} />
        ) : (
          <div className="planning-grid">
            {goals.map((goal) => (
              <GoalCard
                key={goal.id}
                goal={goal}
                canWrite={canWrite}
                onEdit={() => { setEditing(goal); }}
                onContribute={() => { setContributing(goal); }}
              />
            ))}
          </div>
        )}
      </QueryState>
      {editing === null || !canWrite ? null : <GoalDialog goal={editing === "new" ? null : editing} onClose={() => { setEditing(null); }} />}
      {contributing === null ? null : <ContributionsDialog goal={contributing} canWrite={canWrite} onClose={() => { setContributing(null); }} />}
    </PlanningPage>
  );
}
