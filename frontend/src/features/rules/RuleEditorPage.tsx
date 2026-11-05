// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { queryRoots } from "../../lib/queryKeys";
import { PlanningPage, QueryState, ErrorLine } from "../planning/PlanningPage";
import { nameLookup, useAccounts, useCategories, usePayees, useTags } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { useRouteParam } from "../planning/useRouteParam";
import { createRule, getRule, ruleKeys, updateRule } from "./api";
import {
  emptyRule,
  hasProblems,
  parseRule,
  serializeAction,
  serializeConditions,
  serializeRule,
  serializeRuleBody,
  validateRule,
  type RuleDraft,
} from "./builder";
import type { Names } from "./describe";
import { RuleForm } from "./RuleForm";
import { RuleTestPanel } from "./RuleTestPanel";

function useReferences() {
  const categories = useCategories();
  const payees = usePayees();
  const tags = useTags();
  const accounts = useAccounts();
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
  return {
    refs: { categories: categories.data ?? [], payees: payees.data ?? [], tags: tags.data ?? [], accounts: accounts.data ?? [] },
    names,
    loading: categories.isPending || payees.isPending || tags.isPending || accounts.isPending,
    error: categories.error ?? payees.error ?? tags.error ?? accounts.error,
  };
}

function Editor({ ruleId, initial, conditionsEditable }: { ruleId: string | null; initial: RuleDraft; conditionsEditable: boolean }) {
  const navigate = useNavigate();
  const { refs, names, loading, error } = useReferences();
  const [draft, setDraft] = useState(initial);
  const [shown, setShown] = useState(false);
  const problems = validateRule(draft, { conditionsEditable });
  const invalid = hasProblems(problems);
  const save = useAction(
    () => {
      if (ruleId === null) {
        return createRule(serializeRule(draft));
      }
      const doc = serializeRule(draft);
      return updateRule(ruleId, {
        name: doc.name,
        enabled: doc.enabled,
        stop_processing: doc.stop_processing,
        actions: draft.actions.map(serializeAction),
        ...(conditionsEditable ? { conditions: serializeConditions(draft.conditions) } : {}),
      });
    },
    { invalidate: [queryRoots.rules], success: "Rule saved" },
  );
  const submit = async (): Promise<void> => {
    setShown(true);
    if (invalid) {
      return;
    }
    const done = await save.run();
    if (done !== undefined) {
      await navigate({ to: "/rules" });
    }
  };
  // A rule whose conditions the builder cannot show is tested by id (as stored).
  const target = () => (ruleId !== null && !conditionsEditable ? { rule_id: ruleId } : { rule: serializeRuleBody(draft) });
  return (
    <QueryState loading={loading} error={error} label="rule options">
      <RuleForm
        draft={draft}
        problems={shown ? problems : { conditions: {}, actions: {} }}
        refs={refs}
        conditionsEditable={conditionsEditable}
        onChange={setDraft}
      />
      <ErrorLine message={save.error} />
      <div className="planning-actions">
        <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
          Save rule
        </Button>
        <Link to="/rules" className="btn btn--ghost btn--md">
          Cancel
        </Link>
      </div>
      <RuleTestPanel target={target} disabled={invalid} names={names} />
    </QueryState>
  );
}

export function NewRulePage() {
  return (
    <PlanningPage title="New rule">
      <p>
        <Link to="/rules" className="link-action link-action--back">
          All rules
        </Link>
      </p>
      <Editor ruleId={null} initial={emptyRule()} conditionsEditable />
    </PlanningPage>
  );
}

export function EditRulePage() {
  const ruleId = useRouteParam("ruleId");
  const query = useQuery({ queryKey: ruleKeys.one(ruleId), queryFn: () => getRule(ruleId) });
  const rule = query.data;
  const parsed = useMemo(
    () => (rule === undefined ? null : parseRule({ ...rule, conditions: rule.conditions, actions: rule.actions })),
    [rule],
  );
  return (
    <PlanningPage title={rule?.name ?? "Rule"}>
      <p>
        <Link to="/rules" className="link-action link-action--back">
          All rules
        </Link>
      </p>
      <QueryState loading={query.isPending} error={query.error} label="the rule">
        {rule === undefined || parsed === null ? null : rule.redacted ? (
          <Card>
            <p>Parts of this rule refer to accounts you cannot see, so only an owner or admin can edit it.</p>
          </Card>
        ) : (
          <Editor
            key={rule.id + rule.updated_at}
            ruleId={rule.id}
            initial={parsed.draft}
            conditionsEditable={parsed.conditionsSupported}
          />
        )}
      </QueryState>
    </PlanningPage>
  );
}
