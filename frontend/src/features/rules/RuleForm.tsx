// SPDX-License-Identifier: AGPL-3.0-or-later
/** The rule builder form: conditions (all/any, optionally negated) and actions. */
import type { ReactNode } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select, type SelectOption } from "../../components/Select";
import { categoryOptions } from "../categories/tree";
import type { Account, Category, Payee, Tag } from "../planning/queries";
import {
  ACTION_TYPES,
  CONDITION_TYPES,
  TEXT_OPS,
  newAction,
  newCondition,
  newSplitLine,
  type ActionDraft,
  type ActionType,
  type ConditionDraft,
  type ConditionType,
  type RuleDraft,
  type RuleProblems,
  type SplitLineDraft,
  type TextOp,
} from "./builder";

export interface RuleReferences {
  categories: readonly Category[];
  payees: readonly Payee[];
  tags: readonly Tag[];
  accounts: readonly Account[];
}

function isConditionType(value: string): value is ConditionType {
  return CONDITION_TYPES.some((type) => type.value === value);
}

function isActionType(value: string): value is ActionType {
  return ACTION_TYPES.some((type) => type.value === value);
}

function isTextOp(value: string): value is TextOp {
  return TEXT_OPS.some((op) => op.value === value);
}

function MultiSelect({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly SelectOption[];
  value: readonly string[];
  onChange: (ids: string[]) => void;
}) {
  return (
    <Field label={label} hint="Hold Ctrl or Cmd to choose several.">
      <Select
        multiple
        size={Math.min(6, Math.max(3, options.length))}
        value={[...value]}
        options={options}
        onChange={(event) => {
          onChange(Array.from(event.target.selectedOptions, (option) => option.value));
        }}
      />
    </Field>
  );
}

function ConditionRow({
  index,
  item,
  problem,
  refs,
  onChange,
  onRemove,
}: {
  index: number;
  item: ConditionDraft;
  problem: string | undefined;
  refs: RuleReferences;
  onChange: (next: ConditionDraft) => void;
  onRemove: () => void;
}) {
  const n = String(index + 1);
  const patch = (changes: Partial<ConditionDraft>): void => {
    onChange({ ...item, ...changes });
  };
  let body: ReactNode = null;
  switch (item.type) {
    case "payee":
    case "memo":
      body = (
        <>
          <Field label={`Comparison ${n}`}>
            <Select
              value={item.op}
              options={TEXT_OPS}
              onChange={(event) => {
                if (isTextOp(event.target.value)) {
                  patch({ op: event.target.value });
                }
              }}
            />
          </Field>
          <Field label={`Text ${n}`} error={problem}>
            <Input value={item.value} maxLength={200} onChange={(event) => { patch({ value: event.target.value }); }} />
          </Field>
        </>
      );
      break;
    case "payee_is":
      body = (
        <MultiSelect
          label={`Payees ${n}`}
          options={refs.payees.map((payee) => ({ value: payee.id, label: payee.name }))}
          value={item.ids}
          onChange={(ids) => { patch({ ids }); }}
        />
      );
      break;
    case "account":
      body = (
        <MultiSelect
          label={`Accounts ${n}`}
          options={refs.accounts.map((account) => ({ value: account.id, label: account.nickname ?? account.name }))}
          value={item.ids}
          onChange={(ids) => { patch({ ids }); }}
        />
      );
      break;
    case "category_is":
      body = (
        <MultiSelect
          label={`Categories ${n}`}
          options={categoryOptions(refs.categories)}
          value={item.ids}
          onChange={(ids) => { patch({ ids }); }}
        />
      );
      break;
    case "amount":
      body = (
        <>
          <Field label={`Minimum ${n}`}>
            <Input inputMode="decimal" value={item.min} onChange={(event) => { patch({ min: event.target.value }); }} />
          </Field>
          <Field label={`Maximum ${n}`}>
            <Input inputMode="decimal" value={item.max} onChange={(event) => { patch({ max: event.target.value }); }} />
          </Field>
          <Checkbox
            label="Compare the absolute amount"
            checked={item.absolute}
            onChange={(event) => { patch({ absolute: event.target.checked }); }}
          />
        </>
      );
      break;
    case "sign":
      body = (
        <Field label={`Direction ${n}`}>
          <Select
            value={item.sign}
            options={[
              { value: "outflow", label: "Money out (spending)" },
              { value: "inflow", label: "Money in (income)" },
            ]}
            onChange={(event) => { patch({ sign: event.target.value === "inflow" ? "inflow" : "outflow" }); }}
          />
        </Field>
      );
      break;
    case "currency":
      body = (
        <Field label={`Currencies ${n}`} hint="Codes separated by commas, such as USD, EUR.">
          <Input value={item.currencies} onChange={(event) => { patch({ currencies: event.target.value }); }} />
        </Field>
      );
      break;
    case "date":
      body = (
        <>
          <Field label={`From ${n}`}>
            <Input type="date" value={item.from} onChange={(event) => { patch({ from: event.target.value }); }} />
          </Field>
          <Field label={`To ${n}`}>
            <Input type="date" value={item.to} onChange={(event) => { patch({ to: event.target.value }); }} />
          </Field>
        </>
      );
      break;
    case "category_unset":
      body = null;
      break;
  }
  return (
    <li className="planning-row">
      <Field label={`Condition ${n}`}>
        <Select
          value={item.type}
          options={CONDITION_TYPES}
          onChange={(event) => {
            if (isConditionType(event.target.value)) {
              onChange({ ...newCondition(event.target.value), key: item.key, negate: item.negate });
            }
          }}
        />
      </Field>
      {body}
      {problem !== undefined && item.type !== "payee" && item.type !== "memo" ? (
        <p role="alert" className="auth-error">
          {problem}
        </p>
      ) : null}
      <Checkbox label="Not" checked={item.negate} onChange={(event) => { patch({ negate: event.target.checked }); }} />
      <Button aria-label={`Remove condition ${n}`} onClick={onRemove}>
        Remove
      </Button>
    </li>
  );
}

function SplitLines({
  action,
  refs,
  onChange,
}: {
  action: ActionDraft;
  refs: RuleReferences;
  onChange: (lines: SplitLineDraft[]) => void;
}) {
  const percent = action.type === "split_percent";
  const patch = (key: number, changes: Partial<SplitLineDraft>): void => {
    onChange(action.lines.map((line) => (line.key === key ? { ...line, ...changes } : line)));
  };
  return (
    <div className="planning-form">
      {action.lines.map((line, index) => (
        <div className="planning-row" key={line.key}>
          <Field label={`Split category ${String(index + 1)}`}>
            <Select
              value={line.categoryId}
              placeholder="Uncategorised"
              options={categoryOptions(refs.categories)}
              onChange={(event) => { patch(line.key, { categoryId: event.target.value }); }}
            />
          </Field>
          <Field label={percent ? `Split percent ${String(index + 1)}` : `Split amount ${String(index + 1)}`} hint={percent ? undefined : "Empty takes the remainder."}>
            <Input inputMode="decimal" value={line.amount} onChange={(event) => { patch(line.key, { amount: event.target.value }); }} />
          </Field>
          <Field label={`Split memo ${String(index + 1)}`}>
            <Input value={line.memo} onChange={(event) => { patch(line.key, { memo: event.target.value }); }} />
          </Field>
          <Button
            aria-label={`Remove split line ${String(index + 1)}`}
            onClick={() => { onChange(action.lines.filter((entry) => entry.key !== line.key)); }}
          >
            Remove
          </Button>
        </div>
      ))}
      <div>
        <Button onClick={() => { onChange([...action.lines, newSplitLine()]); }}>Add split line</Button>
      </div>
    </div>
  );
}

function ActionRow({
  index,
  action,
  problem,
  refs,
  onChange,
  onRemove,
}: {
  index: number;
  action: ActionDraft;
  problem: string | undefined;
  refs: RuleReferences;
  onChange: (next: ActionDraft) => void;
  onRemove: () => void;
}) {
  const n = String(index + 1);
  let body: ReactNode = null;
  switch (action.type) {
    case "set_category":
      body = (
        <Field label={`Category ${n}`}>
          <Select
            value={action.categoryId}
            placeholder="Choose a category"
            options={categoryOptions(refs.categories)}
            onChange={(event) => { onChange({ ...action, categoryId: event.target.value }); }}
          />
        </Field>
      );
      break;
    case "set_payee":
      body = (
        <Field label={`Payee ${n}`}>
          <Select
            value={action.payeeId}
            placeholder="Choose a payee"
            options={refs.payees.map((payee) => ({ value: payee.id, label: payee.name }))}
            onChange={(event) => { onChange({ ...action, payeeId: event.target.value }); }}
          />
        </Field>
      );
      break;
    case "add_tags":
      body = (
        <MultiSelect
          label={`Tags ${n}`}
          options={refs.tags.map((tag) => ({ value: tag.id, label: tag.name }))}
          value={action.tagIds}
          onChange={(tagIds) => { onChange({ ...action, tagIds }); }}
        />
      );
      break;
    case "set_note":
      body = (
        <Field label={`Note ${n}`}>
          <Input value={action.note} maxLength={2000} onChange={(event) => { onChange({ ...action, note: event.target.value }); }} />
        </Field>
      );
      break;
    case "split_percent":
    case "split_amount":
      body = <SplitLines action={action} refs={refs} onChange={(lines) => { onChange({ ...action, lines }); }} />;
      break;
    case "flag_for_review":
      body = <p className="muted">Marks the transaction as needing review.</p>;
      break;
  }
  return (
    <li className="planning-row">
      <Field label={`Action ${n}`}>
        <Select
          value={action.type}
          options={ACTION_TYPES}
          onChange={(event) => {
            if (isActionType(event.target.value)) {
              onChange({ ...newAction(event.target.value), key: action.key });
            }
          }}
        />
      </Field>
      {body}
      {problem === undefined ? null : (
        <p role="alert" className="auth-error">
          {problem}
        </p>
      )}
      <Button aria-label={`Remove action ${n}`} onClick={onRemove}>
        Remove
      </Button>
    </li>
  );
}

export function RuleForm({
  draft,
  problems,
  refs,
  conditionsEditable,
  onChange,
}: {
  draft: RuleDraft;
  problems: RuleProblems;
  refs: RuleReferences;
  conditionsEditable: boolean;
  onChange: (next: RuleDraft) => void;
}) {
  const { conditions } = draft;
  return (
    <div className="planning-form">
      <Field label="Rule name" error={problems.name} required>
        <Input value={draft.name} maxLength={100} onChange={(event) => { onChange({ ...draft, name: event.target.value }); }} />
      </Field>
      <div className="planning-row">
        <Checkbox label="Enabled" checked={draft.enabled} onChange={(event) => { onChange({ ...draft, enabled: event.target.checked }); }} />
        <Checkbox
          label="Stop processing later rules after this one matches"
          checked={draft.stopProcessing}
          onChange={(event) => { onChange({ ...draft, stopProcessing: event.target.checked }); }}
        />
      </div>

      <fieldset className="planning-form">
        <legend>When</legend>
        {conditionsEditable ? (
          <>
            <Field label="Match">
              <Select
                value={conditions.match}
                options={[
                  { value: "all", label: "All of these conditions" },
                  { value: "any", label: "Any of these conditions" },
                ]}
                onChange={(event) => { onChange({ ...draft, conditions: { ...conditions, match: event.target.value === "any" ? "any" : "all" } }); }}
              />
            </Field>
            <ul className="planning-list" aria-label="Conditions">
              {conditions.items.map((item, index) => (
                <ConditionRow
                  key={item.key}
                  index={index}
                  item={item}
                  problem={problems.conditions[item.key]}
                  refs={refs}
                  onChange={(next) => { onChange({ ...draft, conditions: { ...conditions, items: conditions.items.map((entry) => (entry.key === item.key ? next : entry)) } }); }}
                  onRemove={() => { onChange({ ...draft, conditions: { ...conditions, items: conditions.items.filter((entry) => entry.key !== item.key) } }); }}
                />
              ))}
            </ul>
            {conditions.items.length === 0 ? <p className="muted">No conditions: this rule matches every transaction.</p> : null}
            <div>
              <Button onClick={() => { onChange({ ...draft, conditions: { ...conditions, items: [...conditions.items, newCondition()] } }); }}>
                Add condition
              </Button>
            </div>
          </>
        ) : (
          <p role="note">
            This rule nests conditions more deeply than the builder shows, so they cannot be edited here. They are kept as they are.
          </p>
        )}
      </fieldset>

      <fieldset className="planning-form">
        <legend>Then</legend>
        <ul className="planning-list" aria-label="Actions">
          {draft.actions.map((action, index) => (
            <ActionRow
              key={action.key}
              index={index}
              action={action}
              problem={problems.actions[action.key]}
              refs={refs}
              onChange={(next) => { onChange({ ...draft, actions: draft.actions.map((entry) => (entry.key === action.key ? next : entry)) }); }}
              onRemove={() => { onChange({ ...draft, actions: draft.actions.filter((entry) => entry.key !== action.key) }); }}
            />
          ))}
        </ul>
        <div>
          <Button onClick={() => { onChange({ ...draft, actions: [...draft.actions, newAction("add_tags")] }); }}>Add action</Button>
        </div>
      </fieldset>
      {problems.general === undefined ? null : (
        <p role="alert" className="auth-error">
          {problems.general}
        </p>
      )}
    </div>
  );
}
