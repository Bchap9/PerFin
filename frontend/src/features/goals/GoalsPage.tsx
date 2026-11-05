// SPDX-License-Identifier: AGPL-3.0-or-later
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
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, todayIsoDate } from "../../lib/dates";
import { isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { useHouseholdCurrency } from "../planning/currency";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
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
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save goal
          </Button>
        </>
      }
    >
      <div className="planning-form">
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
      </div>
    </Dialog>
  );
}

function ContributionsDialog({ goal, onClose }: { goal: Goal; onClose: () => void }) {
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
  const columns: TableColumn<Contribution>[] = [
    { id: "date", header: "Date", rowHeader: true, cell: (row) => formatDate(row.contributed_on) ?? row.contributed_on },
    { id: "amount", header: "Amount", align: "end", cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="none" /> },
    { id: "memo", header: "Memo", cell: (row) => row.memo ?? "" },
    {
      id: "remove",
      header: <span className="visually-hidden">Remove</span>,
      align: "end",
      cell: (row) => (
        <Button size="sm" variant="danger" aria-label={`Remove contribution of ${row.amount.amount} on ${row.contributed_on}`} onClick={() => void remove.run(row.id)}>
          Remove
        </Button>
      ),
    },
  ];
  return (
    <Dialog open onClose={onClose} size="lg" title={`Contributions to ${goal.name}`} footer={<Button onClick={onClose}>Close</Button>}>
      <div className="planning-form">
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
        {problem === null ? null : <ErrorLine message={problem} />}
        <ErrorLine message={add.error ?? remove.error} />
        <QueryState loading={query.isPending} error={query.error} label="contributions">
          <Table
            caption="Contributions"
            captionHidden
            columns={columns}
            rows={query.data ?? []}
            rowKey={(row) => row.id}
            maxHeight="md"
            empty="No contributions recorded."
          />
        </QueryState>
      </div>
    </Dialog>
  );
}

function GoalCard({ goal, onEdit, onContribute }: { goal: Goal; onEdit: () => void; onContribute: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const invalidate = [queryRoots.goals, queryRoots.dashboard];
  const archive = useAction(() => updateGoal(goal.id, { archived: !goal.archived }), { invalidate, success: goal.archived ? "Goal restored" : "Goal archived" });
  const remove = useAction(() => deleteGoal(goal.id), { invalidate, success: "Goal deleted" });
  const width = goalBarWidth(goal);
  const { progress } = goal;
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
        <span className="bar" role="img" aria-label={`${goal.name}: ${String(Math.round(width))} percent complete`}>
          <span className="bar__fill" style={{ width: `${String(width)}%` }} />
        </span>
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
          <Button size="sm" onClick={onEdit} aria-label={`Edit ${goal.name}`}>
            Edit
          </Button>
          <Button size="sm" loading={archive.busy} onClick={() => void archive.run()} aria-label={`${goal.archived ? "Restore" : "Archive"} ${goal.name}`}>
            {goal.archived ? "Restore" : "Archive"}
          </Button>
          <Button size="sm" variant="danger" onClick={() => { setConfirming(true); }} aria-label={`Delete ${goal.name}`}>
            Delete
          </Button>
        </div>
        <ErrorLine message={archive.error} />
      </div>
      {confirming ? (
        <Dialog
          open
          onClose={() => { setConfirming(false); }}
          title="Delete goal"
          description={`Delete "${goal.name}" and its contributions?`}
          footer={
            <>
              <Button onClick={() => { setConfirming(false); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run().then((done) => {
                    if (done !== undefined) {
                      setConfirming(false);
                    }
                  });
                }}
              >
                Delete goal
              </Button>
            </>
          }
        >
          <ErrorLine message={remove.error} />
        </Dialog>
      ) : null}
    </Card>
  );
}

export function GoalsPage() {
  const [archived, setArchived] = useState(false);
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
          <Button variant="primary" onClick={() => { setEditing("new"); }}>
            New goal
          </Button>
        </>
      }
    >
      <QueryState loading={query.isPending} error={query.error} label="goals">
        {goals.length === 0 ? (
          <EmptyState title={archived ? "No archived goals" : "No goals yet"} description="Set a target and track what you have saved towards it." />
        ) : (
          <div className="planning-grid">
            {goals.map((goal) => (
              <GoalCard
                key={goal.id}
                goal={goal}
                onEdit={() => { setEditing(goal); }}
                onContribute={() => { setContributing(goal); }}
              />
            ))}
          </div>
        )}
      </QueryState>
      {editing === null ? null : <GoalDialog goal={editing === "new" ? null : editing} onClose={() => { setEditing(null); }} />}
      {contributing === null ? null : <ContributionsDialog goal={contributing} onClose={() => { setContributing(null); }} />}
    </PlanningPage>
  );
}
