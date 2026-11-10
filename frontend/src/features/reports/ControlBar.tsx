// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The filters every report shares: the period (presets first, then custom
 * dates), one account or all of them, and whether transactions still waiting
 * in Review count. They sit in one row above the tabs and scope everything
 * below them, so the figures of the four views always agree.
 */
import "./reports.css";

import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import type { Account } from "../planning/queries";
import { isPeriodId, PERIOD_OPTIONS, rangeProblemText, type DateRange, type PeriodId, type ResolvedPeriod } from "./periods";

export interface ControlValues {
  period: PeriodId;
  from: string;
  to: string;
  account: string;
}

export function accountLabel(account: Pick<Account, "name" | "nickname" | "is_closed">): string {
  const name = account.nickname ?? account.name;
  return account.is_closed ? `${name} (closed)` : name;
}

export function ControlBar({
  values,
  resolved,
  presetDates,
  accounts,
  pendingChecked,
  onChange,
}: {
  values: ControlValues;
  resolved: ResolvedPeriod;
  /** The dates on screen now; custom dates start from them. */
  presetDates: DateRange | null;
  accounts: readonly Account[] | undefined;
  pendingChecked: boolean;
  onChange: (patch: Partial<ControlValues> & { pending?: boolean }) => void;
}) {
  const known = (accounts ?? []).map((account) => ({ value: account.id, label: accountLabel(account) }));
  // Keep the chosen account selected while the list loads (or if it is no longer listed).
  const accountOptions = values.account === "" || known.some((option) => option.value === values.account) ? known : [...known, { value: values.account, label: "Selected account" }];
  const problem = resolved.problem === null ? undefined : rangeProblemText(resolved.problem);
  return (
    <div className="report-controls" role="group" aria-label="Report filters">
      <Field label="Period">
        <Select
          value={values.period}
          options={PERIOD_OPTIONS}
          onChange={(event) => {
            const period = isPeriodId(event.target.value) ? event.target.value : "this-month";
            onChange(period === "custom" && presetDates !== null ? { period, from: presetDates.from, to: presetDates.to } : { period });
          }}
        />
      </Field>
      {values.period === "custom" ? (
        <>
          <Field label="From">
            <Input
              type="date"
              value={values.from}
              aria-invalid={problem === undefined ? undefined : true}
              onChange={(event) => {
                onChange({ from: event.target.value });
              }}
            />
          </Field>
          <Field label="To" error={problem}>
            <Input
              type="date"
              value={values.to}
              onChange={(event) => {
                onChange({ to: event.target.value });
              }}
            />
          </Field>
        </>
      ) : null}
      <Field label="Account">
        <Select
          value={values.account}
          placeholder="All accounts"
          options={accountOptions}
          onChange={(event) => {
            onChange({ account: event.target.value });
          }}
        />
      </Field>
      <div className="report-controls__toggle">
        <Checkbox
          label="Include transactions awaiting review"
          checked={pendingChecked}
          onChange={(event) => {
            onChange({ pending: event.target.checked });
          }}
        />
      </div>
    </div>
  );
}
