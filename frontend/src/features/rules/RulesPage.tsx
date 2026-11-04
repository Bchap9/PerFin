// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Table, type TableColumn } from "../../components/Table";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { nameLookup, useAccounts, useCategories, usePayees, useTags } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { applyRules, deleteRule, listRules, movedOrder, reorderRules, ruleKeys, updateRule, type Rule } from "./api";
import { parseRule } from "./builder";
import { describeGroup, type Names } from "./describe";
import { JobProgress } from "./JobProgress";

function ApplyDialog({ rules, onClose }: { rules: readonly Rule[]; onClose: () => void }) {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [only, setOnly] = useState<string[]>([]);
  const [jobId, setJobId] = useState<string | null>(null);
  const [finished, setFinished] = useState(false);
  const start = useAction(applyRules, { invalidate: [queryRoots.rules] });
  const run = async (): Promise<void> => {
    const done = await start.run({
      ...(from === "" ? {} : { date_from: from }),
      ...(to === "" ? {} : { date_to: to }),
      ...(only.length === 0 ? {} : { rule_ids: only }),
    });
    if (done !== undefined) {
      setJobId(done.value.job_id);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Apply rules to existing transactions"
      description="Runs your enabled rules, in order, over transactions already in the ledger. Use Test on a rule first to preview the changes."
      footer={
        jobId === null ? (
          <>
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={start.busy} onClick={() => void run()}>
              Apply rules
            </Button>
          </>
        ) : (
          <Button variant="primary" onClick={onClose}>
            {finished ? "Done" : "Close"}
          </Button>
        )
      }
    >
      {jobId === null ? (
        <div className="planning-form">
          <div className="planning-row">
            <Field label="From date" hint="Optional.">
              <Input type="date" value={from} onChange={(event) => { setFrom(event.target.value); }} />
            </Field>
            <Field label="To date" hint="Optional.">
              <Input type="date" value={to} onChange={(event) => { setTo(event.target.value); }} />
            </Field>
          </div>
          <fieldset className="planning-form">
            <legend>Rules (none chosen means all enabled rules)</legend>
            {rules.map((rule) => (
              <Checkbox
                key={rule.id}
                label={rule.name}
                checked={only.includes(rule.id)}
                onChange={(event) => { setOnly(event.target.checked ? [...only, rule.id] : only.filter((id) => id !== rule.id)); }}
              />
            ))}
          </fieldset>
          <ErrorLine message={start.error} />
        </div>
      ) : (
        <JobProgress
          jobId={jobId}
          onFinished={() => {
            setFinished(true);
          }}
        />
      )}
    </Dialog>
  );
}

export function RulesPage() {
  const query = useQuery({ queryKey: ruleKeys.list, queryFn: listRules });
  const categories = useCategories();
  const payees = usePayees();
  const tags = useTags();
  const accounts = useAccounts();
  const [applying, setApplying] = useState(false);
  const [deleting, setDeleting] = useState<Rule | null>(null);
  const rules = useMemo(() => [...(query.data ?? [])].sort((a, b) => a.sort_order - b.sort_order), [query.data]);
  const names: Names = useMemo(() => {
    const lookups = {
      category: nameLookup(categories.data),
      payee: nameLookup(payees.data),
      tag: nameLookup(tags.data),
      account: nameLookup(accounts.data?.map((account) => ({ id: account.id, name: account.nickname ?? account.name }))),
    };
    // eslint-disable-next-line security/detect-object-injection -- keyed by the closed kind union
    return (kind, id) => lookups[kind](id);
  }, [categories.data, payees.data, tags.data, accounts.data]);
  const reorder = useAction(reorderRules, { invalidate: [ruleKeys.list] });
  const toggle = useAction((rule: Rule) => updateRule(rule.id, { enabled: !rule.enabled }), { invalidate: [ruleKeys.list] });
  const remove = useAction(deleteRule, { invalidate: [queryRoots.rules], success: "Rule deleted" });
  const ids = rules.map((rule) => rule.id);

  const columns: TableColumn<Rule>[] = [
    {
      id: "name",
      header: "Rule",
      rowHeader: true,
      cell: (rule) => (
        <Link to="/rules/$ruleId" params={{ ruleId: rule.id }}>
          {rule.name}
        </Link>
      ),
    },
    {
      id: "when",
      header: "When",
      cell: (rule) => {
        if (rule.redacted) {
          return <span className="muted">Hidden (refers to accounts you cannot see)</span>;
        }
        const parsed = parseRule({ ...rule, conditions: rule.conditions, actions: rule.actions });
        return parsed.conditionsSupported ? describeGroup(parsed.draft.conditions, names) : "Complex conditions";
      },
    },
    { id: "hits", header: "Matches", align: "end", cell: (rule) => String(rule.hit_count) },
    {
      id: "state",
      header: "Status",
      cell: (rule) => (
        <>
          <Badge tone={rule.enabled ? "success" : "neutral"}>{rule.enabled ? "Enabled" : "Disabled"}</Badge>
          {rule.stop_processing ? <Badge tone="info">Stops here</Badge> : null}
        </>
      ),
    },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (rule) => {
        const up = movedOrder(ids, rule.id, -1);
        const down = movedOrder(ids, rule.id, 1);
        return (
          <span className="planning-actions">
            <Button size="sm" aria-label={`Move ${rule.name} up`} disabled={up === null} onClick={() => up !== null && void reorder.run(up)}>
              Up
            </Button>
            <Button size="sm" aria-label={`Move ${rule.name} down`} disabled={down === null} onClick={() => down !== null && void reorder.run(down)}>
              Down
            </Button>
            <Button size="sm" aria-label={`${rule.enabled ? "Disable" : "Enable"} ${rule.name}`} onClick={() => void toggle.run(rule)}>
              {rule.enabled ? "Disable" : "Enable"}
            </Button>
            <Button size="sm" variant="danger" aria-label={`Delete ${rule.name}`} onClick={() => { setDeleting(rule); }}>
              Delete
            </Button>
          </span>
        );
      },
    },
  ];

  return (
    <PlanningPage
      title="Rules"
      actions={
        <>
          <Button disabled={rules.length === 0} onClick={() => { setApplying(true); }}>
            Apply to existing transactions
          </Button>
          <Link to="/rules/new" className="btn btn--primary btn--md">
            New rule
          </Link>
        </>
      }
    >
      <p className="muted">Rules run top to bottom on every new transaction. Move a rule up to run it earlier.</p>
      <ErrorLine message={reorder.error ?? toggle.error} />
      <QueryState loading={query.isPending} error={query.error} label="rules">
        <Table
          caption="Rules in the order they run"
          captionHidden
          columns={columns}
          rows={rules}
          rowKey={(rule) => rule.id}
          maxHeight="none"
          empty={<EmptyState title="No rules yet" description="A rule categorises or tags transactions that match its conditions." headingLevel={3} />}
        />
      </QueryState>
      {applying ? <ApplyDialog rules={rules.filter((rule) => !rule.redacted)} onClose={() => { setApplying(false); }} /> : null}
      {deleting === null ? null : (
        <Dialog
          open
          onClose={() => { setDeleting(null); }}
          title="Delete rule"
          description={`Delete "${deleting.name}"? Transactions it already changed keep their changes.`}
          footer={
            <>
              <Button onClick={() => { setDeleting(null); }}>Cancel</Button>
              <Button
                variant="danger"
                loading={remove.busy}
                onClick={() => {
                  void remove.run(deleting.id).then((done) => {
                    if (done !== undefined) {
                      setDeleting(null);
                    }
                  });
                }}
              >
                Delete rule
              </Button>
            </>
          }
        >
          <ErrorLine message={remove.error} />
        </Dialog>
      )}
    </PlanningPage>
  );
}
