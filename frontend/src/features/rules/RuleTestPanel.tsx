// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Dry run (`POST /rules/test`): which existing transactions the rule being
 * edited would match and what it would change, before anything is saved.
 */
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { formatDate } from "../../lib/dates";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { testRule, type RuleTestBody, type RuleTestItem, type RuleTestResult } from "./api";
import { describeChanges, type Names } from "./describe";

const PAGE = 25;

export function RuleTestPanel({
  target,
  disabled,
  names,
}: {
  /** The rule to test: its body (unsaved edits) or the id of a saved rule. */
  target: () => Pick<RuleTestBody, "rule" | "rule_id">;
  disabled: boolean;
  names: Names;
}) {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [onlyChanges, setOnlyChanges] = useState(true);
  const [items, setItems] = useState<RuleTestItem[]>([]);
  const [summary, setSummary] = useState<Pick<RuleTestResult, "matched" | "scanned" | "next_cursor"> | null>(null);
  const run = useAction((cursor: string | null) =>
    testRule({
      ...target(),
      limit: PAGE,
      only_changes: onlyChanges,
      ...(from === "" ? {} : { date_from: from }),
      ...(to === "" ? {} : { date_to: to }),
      ...(cursor === null ? {} : { cursor }),
    }),
  );

  const start = async (cursor: string | null): Promise<void> => {
    const done = await run.run(cursor);
    if (done !== undefined) {
      setItems(cursor === null ? done.value.items : [...items, ...done.value.items]);
      setSummary({
        matched: (cursor === null ? 0 : (summary?.matched ?? 0)) + done.value.matched,
        scanned: (cursor === null ? 0 : (summary?.scanned ?? 0)) + done.value.scanned,
        next_cursor: done.value.next_cursor,
      });
    }
  };

  return (
    <Card title="Test this rule" headingLevel={2}>
      <div className="planning-form">
        <p className="muted">Nothing is changed. This shows what the rule would do to existing transactions.</p>
        <div className="planning-row">
          <Field label="Test from">
            <Input type="date" value={from} onChange={(event) => { setFrom(event.target.value); }} />
          </Field>
          <Field label="Test to">
            <Input type="date" value={to} onChange={(event) => { setTo(event.target.value); }} />
          </Field>
          <Checkbox label="Only show transactions that would change" checked={onlyChanges} onChange={(event) => { setOnlyChanges(event.target.checked); }} />
          <Button loading={run.busy} disabled={disabled} onClick={() => void start(null)}>
            Run test
          </Button>
        </div>
        <ErrorLine message={run.error} />
        {summary === null ? null : (
          <>
            <p role="status">
              {summary.matched} of {summary.scanned} transactions scanned would match.
            </p>
            {items.length === 0 ? null : (
              <ul className="planning-list" aria-label="Matching transactions">
                {items.map((item) => {
                  const changes = describeChanges(item.changes, names);
                  return (
                    <li key={item.transaction.id} className="planning-list__item">
                      <span>
                        {formatDate(item.transaction.posted_date) ?? item.transaction.posted_date}{" "}
                        {item.transaction.raw_payee ?? item.transaction.memo ?? "Transaction"}{" "}
                        <MoneyText amount={item.transaction.amount.amount} currency={item.transaction.amount.currency} />
                      </span>
                      <span>
                        {changes.length === 0 ? <Badge>No change</Badge> : changes.map((line) => <span key={line} className="planning-diff">{line}. </span>)}
                        {item.skipped.map((skipped) => (
                          <Badge key={`${skipped.rule_id}-${skipped.action}`} tone="warning">
                            {skipped.action} skipped: {skipped.reason}
                          </Badge>
                        ))}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
            {summary.next_cursor === null ? null : (
              <div>
                <Button loading={run.busy} onClick={() => void start(summary.next_cursor)}>
                  Scan more transactions
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </Card>
  );
}
