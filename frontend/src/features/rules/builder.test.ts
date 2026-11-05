// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  emptyRule,
  hasProblems,
  newAction,
  newCondition,
  parseConditions,
  parseRule,
  serializeCondition,
  serializeConditions,
  serializeRule,
  serializeRuleBody,
  validateRule,
  type ActionDraft,
  type ConditionDoc,
  type ConditionDraft,
  type RuleDraft,
} from "./builder";
import { describeChanges, describeGroup, type Names } from "./describe";
import { movedOrder } from "./api";

const CAT_A = "11111111-1111-4111-8111-111111111111";
const CAT_B = "22222222-2222-4222-8222-222222222222";
const PAYEE = "33333333-3333-4333-8333-333333333333";
const TAG = "44444444-4444-4444-8444-444444444444";

function condition(patch: Partial<ConditionDraft>): ConditionDraft {
  return { ...newCondition(), ...patch };
}

function action(patch: Partial<ActionDraft>): ActionDraft {
  return { ...newAction(), ...patch };
}

function rule(patch: Partial<RuleDraft>): RuleDraft {
  return {
    name: "Groceries",
    enabled: true,
    stopProcessing: false,
    conditions: { match: "all", items: [condition({ type: "payee", op: "contains", value: "  market " })] },
    actions: [action({ type: "set_category", categoryId: CAT_A })],
    ...patch,
  };
}

describe("serialising conditions", () => {
  it("writes each condition type in the server's document form", () => {
    expect(serializeCondition(condition({ type: "payee", op: "starts_with", value: " ACME " }))).toEqual({ type: "payee", op: "starts_with", value: "ACME" });
    expect(serializeCondition(condition({ type: "memo", op: "glob", value: "rent*" }))).toEqual({ type: "memo", op: "glob", value: "rent*" });
    expect(serializeCondition(condition({ type: "payee_is", ids: [PAYEE] }))).toEqual({ type: "payee_is", payee_ids: [PAYEE] });
    expect(serializeCondition(condition({ type: "account", ids: [CAT_A] }))).toEqual({ type: "account", account_ids: [CAT_A] });
    expect(serializeCondition(condition({ type: "category_is", ids: [CAT_B] }))).toEqual({ type: "category_is", category_ids: [CAT_B] });
    expect(serializeCondition(condition({ type: "sign", sign: "inflow" }))).toEqual({ type: "sign", sign: "inflow" });
    expect(serializeCondition(condition({ type: "category_unset" }))).toEqual({ type: "category_unset" });
    expect(serializeCondition(condition({ type: "currency", currencies: "usd, eur usd" }))).toEqual({ type: "currency", currencies: ["USD", "EUR"] });
    expect(serializeCondition(condition({ type: "date", from: "2026-01-01", to: "" }))).toEqual({ type: "date", date_from: "2026-01-01" });
  });

  it("keeps amounts as decimal strings and leaves out empty bounds", () => {
    expect(serializeCondition(condition({ type: "amount", min: "10.50", max: "", absolute: true }))).toEqual({
      type: "amount",
      min: "10.50",
      absolute: true,
    });
  });

  it("wraps a negated condition in not and groups by all or any", () => {
    const negated = condition({ type: "sign", sign: "outflow", negate: true });
    expect(serializeCondition(negated)).toEqual({ type: "not", condition: { type: "sign", sign: "outflow" } });
    expect(serializeConditions({ match: "any", items: [negated, condition({ type: "category_unset" })] })).toEqual({
      type: "any",
      conditions: [{ type: "not", condition: { type: "sign", sign: "outflow" } }, { type: "category_unset" }],
    });
  });
});

describe("serialising actions and whole rules", () => {
  it("writes the create body", () => {
    const body = serializeRule(
      rule({
        stopProcessing: true,
        actions: [
          action({ type: "add_tags", tagIds: [TAG] }),
          action({ type: "set_payee", payeeId: PAYEE }),
          action({ type: "set_note", note: " check this " }),
          action({ type: "flag_for_review" }),
        ],
      }),
    );
    expect(body).toEqual({
      name: "Groceries",
      enabled: true,
      stop_processing: true,
      conditions: { type: "all", conditions: [{ type: "payee", op: "contains", value: "market" }] },
      actions: [
        { type: "add_tags", tag_ids: [TAG] },
        { type: "set_payee", payee_id: PAYEE },
        { type: "set_note", note: "check this" },
        { type: "flag_for_review" },
      ],
    });
  });

  it("writes percent splits as strings and amount splits with a remainder line", () => {
    const percent = newAction("split_percent");
    percent.lines = [
      { key: 1, categoryId: CAT_A, amount: "60", memo: "" },
      { key: 2, categoryId: "", amount: "40.5", memo: " rest " },
    ];
    expect(serializeRuleBody(rule({ actions: [percent] })).actions).toEqual([
      {
        type: "split_percent",
        lines: [
          { category_id: CAT_A, percent: "60" },
          { category_id: null, percent: "40.5", memo: "rest" },
        ],
      },
    ]);
    const amounts = newAction("split_amount");
    amounts.lines = [
      { key: 1, categoryId: CAT_A, amount: "25.00", memo: "" },
      { key: 2, categoryId: CAT_B, amount: "", memo: "" },
    ];
    expect(serializeRuleBody(rule({ actions: [amounts] })).actions).toEqual([
      {
        type: "split_amount",
        lines: [
          { category_id: CAT_A, amount: "25.00" },
          { category_id: CAT_B, amount: null },
        ],
      },
    ]);
  });

  it("the dry-run body has no name or enabled flag", () => {
    expect(Object.keys(serializeRuleBody(rule({}))).sort()).toEqual(["actions", "conditions", "stop_processing"]);
  });
});

describe("parsing stored rules", () => {
  it("round-trips what the builder writes", () => {
    const original = rule({
      conditions: {
        match: "any",
        items: [
          condition({ type: "payee", op: "equals", value: "ACME" }),
          condition({ type: "amount", min: "5", max: "50", absolute: true, negate: true }),
          condition({ type: "currency", currencies: "USD, EUR" }),
        ],
      },
      actions: [action({ type: "add_tags", tagIds: [TAG] }), action({ type: "set_note", note: "hi" })],
    });
    const doc = serializeRule(original);
    const { draft, conditionsSupported } = parseRule({
      name: doc.name,
      enabled: doc.enabled,
      stop_processing: doc.stop_processing,
      conditions: doc.conditions,
      actions: doc.actions,
    });
    expect(conditionsSupported).toBe(true);
    expect(serializeRule(draft)).toEqual(doc);
  });

  it("reads a lone condition as a one-item all group", () => {
    expect(parseConditions({ type: "category_unset" })?.match).toBe("all");
    expect(parseConditions({ type: "category_unset" })?.items).toHaveLength(1);
  });

  it("refuses nesting deeper than the builder shows", () => {
    const nested: ConditionDoc = {
      type: "all",
      conditions: [{ type: "any", conditions: [{ type: "category_unset" }] }],
    };
    expect(parseConditions(nested)).toBeNull();
    const { conditionsSupported } = parseRule({ name: "n", enabled: true, stop_processing: false, conditions: nested, actions: [{ type: "flag_for_review" }] });
    expect(conditionsSupported).toBe(false);
  });
});

describe("validation", () => {
  it("accepts a complete rule", () => {
    expect(hasProblems(validateRule(rule({})))).toBe(false);
    expect(hasProblems(validateRule(emptyRule()))).toBe(true);
  });

  it("flags missing values", () => {
    const draft = rule({
      name: " ",
      conditions: { match: "all", items: [condition({ type: "payee", value: "" }), condition({ type: "payee_is", ids: [] })] },
      actions: [action({ type: "set_category", categoryId: "" })],
    });
    const problems = validateRule(draft);
    expect(problems.name).toBeDefined();
    expect(Object.keys(problems.conditions)).toHaveLength(2);
    expect(Object.keys(problems.actions)).toHaveLength(1);
  });

  it("checks amount bounds", () => {
    const check = (patch: Partial<ConditionDraft>) => Object.values(validateRule(rule({ conditions: { match: "all", items: [condition({ type: "amount", ...patch })] } })).conditions);
    expect(check({ min: "", max: "" })).toHaveLength(1);
    expect(check({ min: "abc" })).toHaveLength(1);
    expect(check({ min: "10", max: "5" })).toHaveLength(1);
    expect(check({ min: "-5", absolute: true })).toHaveLength(1);
    expect(check({ min: "5", max: "10" })).toHaveLength(0);
  });

  it("requires percent splits to add up to exactly 100", () => {
    const split = (a: string, b: string): ActionDraft => ({
      ...newAction("split_percent"),
      lines: [
        { key: 1, categoryId: CAT_A, amount: a, memo: "" },
        { key: 2, categoryId: CAT_B, amount: b, memo: "" },
      ],
    });
    expect(Object.keys(validateRule(rule({ actions: [split("60", "30")] })).actions)).toHaveLength(1);
    expect(Object.keys(validateRule(rule({ actions: [split("33.3333", "66.6667")] })).actions)).toHaveLength(0);
    expect(Object.keys(validateRule(rule({ actions: [split("100", "0")] })).actions)).toHaveLength(1);
  });

  it("allows one remainder line and not two", () => {
    const lines = (a: string, b: string): ActionDraft => ({
      ...newAction("split_amount"),
      lines: [
        { key: 1, categoryId: CAT_A, amount: a, memo: "" },
        { key: 2, categoryId: CAT_B, amount: b, memo: "" },
      ],
    });
    expect(Object.keys(validateRule(rule({ actions: [lines("10", "")] })).actions)).toHaveLength(0);
    expect(Object.keys(validateRule(rule({ actions: [lines("", "")] })).actions)).toHaveLength(1);
  });

  it("refuses duplicate action kinds and set_category together with a split", () => {
    expect(validateRule(rule({ actions: [action({ type: "flag_for_review" }), action({ type: "flag_for_review" })] })).general).toBeDefined();
    const split = { ...newAction("split_percent"), lines: [{ key: 1, categoryId: "", amount: "50", memo: "" }, { key: 2, categoryId: "", amount: "50", memo: "" }] };
    expect(validateRule(rule({ actions: [action({ type: "set_category", categoryId: CAT_A }), split] })).general).toMatch(/cannot be combined/);
  });

  it("does not check conditions it cannot edit", () => {
    const draft = rule({ conditions: { match: "all", items: [] } });
    expect(hasProblems(validateRule(draft, { conditionsEditable: false }))).toBe(false);
  });
});

describe("plain-language summaries", () => {
  const names: Names = (kind, id) => `${kind}:${id}`;

  it("describes a condition group", () => {
    const group = { match: "any" as const, items: [condition({ type: "payee", op: "contains", value: "cafe" }), condition({ type: "sign", sign: "inflow", negate: true })] };
    expect(describeGroup(group, names)).toBe('Payee text contains "cafe" or not: Money in');
    expect(describeGroup({ match: "all", items: [] }, names)).toBe("Every transaction");
  });

  it("describes what a dry run would change", () => {
    const lines = describeChanges(
      {
        category_id: { before: null, after: CAT_A },
        added_tag_ids: [TAG],
        review_state: { before: "none", after: "pending" },
        splits: { before: [], after: [{ amount: { amount: "-5.00" }, category_id: CAT_B }] },
      },
      names,
    );
    expect(lines).toEqual([
      `Category: none to category:${CAT_A}`,
      `Tags added: tag:${TAG}`,
      "Review: none to pending",
      `Split: not split to -5.00 category:${CAT_B}`,
    ]);
  });
});

describe("reordering", () => {
  it("moves a rule one place and refuses past the ends", () => {
    expect(movedOrder(["a", "b", "c"], "b", -1)).toEqual(["b", "a", "c"]);
    expect(movedOrder(["a", "b", "c"], "b", 1)).toEqual(["a", "c", "b"]);
    expect(movedOrder(["a", "b", "c"], "a", -1)).toBeNull();
    expect(movedOrder(["a", "b", "c"], "c", 1)).toBeNull();
    expect(movedOrder(["a"], "x", 1)).toBeNull();
  });
});
