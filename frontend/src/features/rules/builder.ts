// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The rule builder's model and its (de)serialisation.
 *
 * The editor works on drafts (form-friendly: strings for every input, a
 * stable `key` per row); `serializeRule` turns a draft into exactly the JSON
 * document `POST /rules` takes (backend `app/domain/rules/schemas.py`), and
 * `parseRule` turns a stored rule back into a draft. A stored rule the
 * builder cannot express (nested groups) parses to `null` and the editor then
 * leaves its conditions alone instead of rewriting them.
 */
import { addDecimals, compareDecimals, parseDecimal, tryParseDecimal } from "../../lib/money";
import type { components } from "../../lib/api/schema";

type Schemas = components["schemas"];

export type ConditionDoc = Schemas["RuleCreate"]["conditions"];
export type ActionDoc = Schemas["RuleCreate"]["actions"][number];
export type RuleDoc = Schemas["RuleCreate"];

export type TextOp = "contains" | "equals" | "starts_with" | "ends_with" | "glob";
export const TEXT_OPS: readonly { value: TextOp; label: string }[] = [
  { value: "contains", label: "contains" },
  { value: "equals", label: "is exactly" },
  { value: "starts_with", label: "starts with" },
  { value: "ends_with", label: "ends with" },
  { value: "glob", label: "matches pattern (* and ?)" },
];

export type ConditionType =
  | "payee"
  | "memo"
  | "payee_is"
  | "amount"
  | "sign"
  | "account"
  | "currency"
  | "date"
  | "category_is"
  | "category_unset";

export const CONDITION_TYPES: readonly { value: ConditionType; label: string }[] = [
  { value: "payee", label: "Payee text" },
  { value: "memo", label: "Memo text" },
  { value: "payee_is", label: "Payee is one of" },
  { value: "amount", label: "Amount" },
  { value: "sign", label: "Money in or out" },
  { value: "account", label: "Account is one of" },
  { value: "currency", label: "Currency" },
  { value: "date", label: "Date" },
  { value: "category_is", label: "Category is one of" },
  { value: "category_unset", label: "Has no category" },
];

export interface ConditionDraft {
  key: number;
  type: ConditionType;
  /** Wrap the condition in `not`. */
  negate: boolean;
  op: TextOp;
  value: string;
  ids: string[];
  min: string;
  max: string;
  absolute: boolean;
  sign: "inflow" | "outflow";
  currencies: string;
  from: string;
  to: string;
}

export interface GroupDraft {
  match: "all" | "any";
  items: ConditionDraft[];
}

export type ActionType = "set_category" | "add_tags" | "set_payee" | "split_percent" | "split_amount" | "flag_for_review" | "set_note";

export const ACTION_TYPES: readonly { value: ActionType; label: string }[] = [
  { value: "set_category", label: "Set category" },
  { value: "add_tags", label: "Add tags" },
  { value: "set_payee", label: "Set payee" },
  { value: "split_percent", label: "Split by percentage" },
  { value: "split_amount", label: "Split by amount" },
  { value: "flag_for_review", label: "Flag for review" },
  { value: "set_note", label: "Set note" },
];

export interface SplitLineDraft {
  key: number;
  categoryId: string;
  /** Percent for `split_percent`, amount for `split_amount` (empty: the remainder). */
  amount: string;
  memo: string;
}

export interface ActionDraft {
  key: number;
  type: ActionType;
  categoryId: string;
  payeeId: string;
  tagIds: string[];
  note: string;
  lines: SplitLineDraft[];
}

export interface RuleDraft {
  name: string;
  enabled: boolean;
  stopProcessing: boolean;
  conditions: GroupDraft;
  actions: ActionDraft[];
}

let counter = 0;
export function nextKey(): number {
  counter += 1;
  return counter;
}

export function newCondition(type: ConditionType = "payee"): ConditionDraft {
  return {
    key: nextKey(),
    type,
    negate: false,
    op: "contains",
    value: "",
    ids: [],
    min: "",
    max: "",
    absolute: false,
    sign: "outflow",
    currencies: "",
    from: "",
    to: "",
  };
}

export function newSplitLine(): SplitLineDraft {
  return { key: nextKey(), categoryId: "", amount: "", memo: "" };
}

export function newAction(type: ActionType = "set_category"): ActionDraft {
  return {
    key: nextKey(),
    type,
    categoryId: "",
    payeeId: "",
    tagIds: [],
    note: "",
    lines: type === "split_percent" || type === "split_amount" ? [newSplitLine(), newSplitLine()] : [],
  };
}

export function emptyRule(): RuleDraft {
  return { name: "", enabled: true, stopProcessing: false, conditions: { match: "all", items: [newCondition()] }, actions: [newAction()] };
}

// ---- serialisation -----------------------------------------------------------

function parseCurrencies(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((code) => code.trim().toUpperCase()).filter((code) => code !== ""))];
}

function leafDoc(draft: ConditionDraft): ConditionDoc {
  switch (draft.type) {
    case "payee":
    case "memo":
      return { type: draft.type, op: draft.op, value: draft.value.trim() };
    case "payee_is":
      return { type: "payee_is", payee_ids: draft.ids };
    case "amount":
      return {
        type: "amount",
        ...(draft.min.trim() === "" ? {} : { min: draft.min.trim() }),
        ...(draft.max.trim() === "" ? {} : { max: draft.max.trim() }),
        absolute: draft.absolute,
      };
    case "sign":
      return { type: "sign", sign: draft.sign };
    case "account":
      return { type: "account", account_ids: draft.ids };
    case "currency":
      return { type: "currency", currencies: parseCurrencies(draft.currencies) };
    case "date":
      return {
        type: "date",
        ...(draft.from === "" ? {} : { date_from: draft.from }),
        ...(draft.to === "" ? {} : { date_to: draft.to }),
      };
    case "category_is":
      return { type: "category_is", category_ids: draft.ids };
    case "category_unset":
      return { type: "category_unset" };
  }
}

export function serializeCondition(draft: ConditionDraft): ConditionDoc {
  const leaf = leafDoc(draft);
  return draft.negate ? { type: "not", condition: leaf } : leaf;
}

export function serializeConditions(group: GroupDraft): ConditionDoc {
  return { type: group.match, conditions: group.items.map(serializeCondition) };
}

function splitMemo(line: SplitLineDraft): { memo?: string } {
  return line.memo.trim() === "" ? {} : { memo: line.memo.trim() };
}

export function serializeAction(draft: ActionDraft): ActionDoc {
  switch (draft.type) {
    case "set_category":
      return { type: "set_category", category_id: draft.categoryId };
    case "add_tags":
      return { type: "add_tags", tag_ids: draft.tagIds };
    case "set_payee":
      return { type: "set_payee", payee_id: draft.payeeId };
    case "split_percent":
      return {
        type: "split_percent",
        lines: draft.lines.map((line) => ({
          category_id: line.categoryId === "" ? null : line.categoryId,
          percent: line.amount.trim(),
          ...splitMemo(line),
        })),
      };
    case "split_amount":
      return {
        type: "split_amount",
        lines: draft.lines.map((line) => ({
          category_id: line.categoryId === "" ? null : line.categoryId,
          amount: line.amount.trim() === "" ? null : line.amount.trim(),
          ...splitMemo(line),
        })),
      };
    case "flag_for_review":
      return { type: "flag_for_review" };
    case "set_note":
      return { type: "set_note", note: draft.note.trim() };
  }
}

/** The body of `POST /rules` (and the `rule` of `POST /rules/test`, which has no name). */
export function serializeRule(draft: RuleDraft): RuleDoc {
  return {
    name: draft.name.trim(),
    enabled: draft.enabled,
    stop_processing: draft.stopProcessing,
    conditions: serializeConditions(draft.conditions),
    actions: draft.actions.map(serializeAction),
  };
}

export function serializeRuleBody(draft: RuleDraft): Schemas["RuleBody"] {
  return {
    stop_processing: draft.stopProcessing,
    conditions: serializeConditions(draft.conditions),
    actions: draft.actions.map(serializeAction),
  };
}

// ---- parsing -----------------------------------------------------------------

function leafDraft(doc: ConditionDoc): ConditionDraft | null {
  const draft = newCondition(doc.type === "not" || doc.type === "all" || doc.type === "any" ? "payee" : doc.type);
  switch (doc.type) {
    case "payee":
    case "memo":
      return { ...draft, op: doc.op, value: doc.value };
    case "payee_is":
      return { ...draft, ids: [...doc.payee_ids] };
    case "amount":
      return { ...draft, min: doc.min === null || doc.min === undefined ? "" : String(doc.min), max: doc.max === null || doc.max === undefined ? "" : String(doc.max), absolute: doc.absolute };
    case "sign":
      return { ...draft, sign: doc.sign };
    case "account":
      return { ...draft, ids: [...doc.account_ids] };
    case "currency":
      return { ...draft, currencies: doc.currencies.join(", ") };
    case "date":
      return { ...draft, from: doc.date_from ?? "", to: doc.date_to ?? "" };
    case "category_is":
      return { ...draft, ids: [...doc.category_ids] };
    case "category_unset":
      return draft;
    default:
      return null;
  }
}

function itemDraft(doc: ConditionDoc): ConditionDraft | null {
  if (doc.type === "not") {
    const inner = leafDraft(doc.condition);
    return inner === null ? null : { ...inner, negate: true };
  }
  return leafDraft(doc);
}

/** A stored condition tree as a draft; `null` if it nests deeper than the builder shows. */
export function parseConditions(doc: ConditionDoc): GroupDraft | null {
  if (doc.type === "all" || doc.type === "any") {
    const items: ConditionDraft[] = [];
    for (const child of doc.conditions ?? []) {
      const item = itemDraft(child);
      if (item === null) {
        return null;
      }
      items.push(item);
    }
    return { match: doc.type, items };
  }
  const single = itemDraft(doc);
  return single === null ? null : { match: "all", items: [single] };
}

export function parseAction(doc: ActionDoc): ActionDraft {
  const draft = newAction(doc.type);
  switch (doc.type) {
    case "set_category":
      return { ...draft, categoryId: doc.category_id };
    case "add_tags":
      return { ...draft, tagIds: [...doc.tag_ids] };
    case "set_payee":
      return { ...draft, payeeId: doc.payee_id };
    case "split_percent":
      return {
        ...draft,
        lines: doc.lines.map((line) => ({ key: nextKey(), categoryId: line.category_id ?? "", amount: String(line.percent), memo: line.memo ?? "" })),
      };
    case "split_amount":
      return {
        ...draft,
        lines: doc.lines.map((line) => ({
          key: nextKey(),
          categoryId: line.category_id ?? "",
          amount: line.amount === null || line.amount === undefined ? "" : String(line.amount),
          memo: line.memo ?? "",
        })),
      };
    case "set_note":
      return { ...draft, note: doc.note };
    case "flag_for_review":
      return draft;
  }
}

export interface StoredRule {
  name: string;
  enabled: boolean;
  stop_processing: boolean;
  conditions: ConditionDoc;
  actions: ActionDoc[];
}

/** The draft of a stored rule, with `conditions` null when the builder cannot show them. */
export function parseRule(rule: StoredRule): { draft: RuleDraft; conditionsSupported: boolean } {
  const group = parseConditions(rule.conditions);
  return {
    conditionsSupported: group !== null,
    draft: {
      name: rule.name,
      enabled: rule.enabled,
      stopProcessing: rule.stop_processing,
      conditions: group ?? { match: "all", items: [] },
      actions: rule.actions.map(parseAction),
    },
  };
}

// ---- validation --------------------------------------------------------------

export interface RuleProblems {
  name?: string;
  conditions: Record<number, string>;
  actions: Record<number, string>;
  general?: string;
}

function validAmountBound(text: string): boolean {
  return text.trim() === "" || tryParseDecimal(text.trim()) !== null;
}

function conditionProblem(draft: ConditionDraft): string | undefined {
  switch (draft.type) {
    case "payee":
    case "memo":
      return draft.value.trim() === "" ? "Enter the text to look for." : undefined;
    case "payee_is":
    case "account":
    case "category_is":
      return draft.ids.length === 0 ? "Choose at least one." : undefined;
    case "amount": {
      if (draft.min.trim() === "" && draft.max.trim() === "") {
        return "Enter a minimum, a maximum or both.";
      }
      if (!validAmountBound(draft.min) || !validAmountBound(draft.max)) {
        return "Amounts are numbers such as 25 or 99.50.";
      }
      if (draft.min.trim() !== "" && draft.max.trim() !== "" && compareDecimals(parseDecimal(draft.min.trim()), parseDecimal(draft.max.trim())) > 0) {
        return "The minimum is above the maximum.";
      }
      if (draft.absolute && (draft.min.trim().startsWith("-") || draft.max.trim().startsWith("-"))) {
        return "With the absolute amount, bounds cannot be negative.";
      }
      return undefined;
    }
    case "currency": {
      const codes = parseCurrencies(draft.currencies);
      if (codes.length === 0) {
        return "Enter at least one currency code.";
      }
      return codes.every((code) => /^[A-Z]{3}$/.test(code)) ? undefined : "Currency codes have three letters, such as USD.";
    }
    case "date":
      if (draft.from === "" && draft.to === "") {
        return "Choose a start date, an end date or both.";
      }
      return draft.from !== "" && draft.to !== "" && draft.from > draft.to ? "The start date is after the end date." : undefined;
    case "sign":
    case "category_unset":
      return undefined;
  }
}

function actionProblem(draft: ActionDraft): string | undefined {
  switch (draft.type) {
    case "set_category":
      return draft.categoryId === "" ? "Choose a category." : undefined;
    case "set_payee":
      return draft.payeeId === "" ? "Choose a payee." : undefined;
    case "add_tags":
      return draft.tagIds.length === 0 ? "Choose at least one tag." : undefined;
    case "set_note":
      return draft.note.trim() === "" ? "Enter the note." : undefined;
    case "flag_for_review":
      return undefined;
    case "split_percent": {
      if (draft.lines.length < 2) {
        return "A split needs at least two lines.";
      }
      let total = { units: 0n, scale: 0 };
      for (const line of draft.lines) {
        const value = tryParseDecimal(line.amount.trim());
        if (value === null || value.units <= 0n) {
          return "Each line needs a percentage above zero.";
        }
        total = addDecimals(total, value);
      }
      return compareDecimals(total, { units: 100n, scale: 0 }) === 0 ? undefined : "Percentages must add up to exactly 100.";
    }
    case "split_amount": {
      if (draft.lines.length < 2) {
        return "A split needs at least two lines.";
      }
      const remainders = draft.lines.filter((line) => line.amount.trim() === "").length;
      if (remainders > 1) {
        return "Only one line can take the remainder (leave its amount empty).";
      }
      for (const line of draft.lines) {
        if (line.amount.trim() === "") {
          continue;
        }
        const value = tryParseDecimal(line.amount.trim());
        if (value === null || value.units <= 0n) {
          return "Each amount must be above zero.";
        }
      }
      return undefined;
    }
  }
}

/** Mirror of the server's rules so mistakes show before saving; the server checks again. */
export function validateRule(draft: RuleDraft, options: { conditionsEditable?: boolean } = {}): RuleProblems {
  const problems: RuleProblems = { conditions: {}, actions: {} };
  if (draft.name.trim() === "") {
    problems.name = "Enter a name.";
  }
  if (options.conditionsEditable !== false) {
    for (const item of draft.conditions.items) {
      const problem = conditionProblem(item);
      if (problem !== undefined) {
        problems.conditions[item.key] = problem;
      }
    }
    if (draft.conditions.match === "any" && draft.conditions.items.length === 0) {
      problems.general = "Add at least one condition.";
    }
  }
  if (draft.actions.length === 0) {
    problems.general = "Add at least one action.";
  }
  const types = draft.actions.map((action) => action.type);
  if (new Set(types).size !== types.length) {
    problems.general = "Each kind of action can be used once.";
  }
  const categorising = types.filter((type) => type === "set_category" || type === "split_percent" || type === "split_amount");
  if (categorising.length > 1) {
    problems.general = "Set category and the split actions cannot be combined: they write the same thing.";
  }
  for (const action of draft.actions) {
    const problem = actionProblem(action);
    if (problem !== undefined) {
      problems.actions[action.key] = problem;
    }
  }
  return problems;
}

export function hasProblems(problems: RuleProblems): boolean {
  return (
    problems.name !== undefined ||
    problems.general !== undefined ||
    Object.keys(problems.conditions).length > 0 ||
    Object.keys(problems.actions).length > 0
  );
}
