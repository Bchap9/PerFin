// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What a manual asset is worth and how it is valued. `AssetFields` is shared
 * by "Add account" (a manual asset account is made with its asset and first
 * value) and by `NewAssetDialog`, which adds the details to a manual asset
 * account that has none yet.
 */
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, type Account } from "../accounts/api";
import { labelOf } from "../investments/format";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { createAsset, METHODS, METHOD_LABELS, type AssetCreate, type ValuationMethod } from "./api";

export interface AssetForm {
  method: ValuationMethod;
  description: string;
  value: string;
  valuedOn: string;
}

export type AssetProblems = Partial<Record<"value" | "valuedOn", string>>;

export function emptyAssetForm(today: string): AssetForm {
  return { method: "manual", description: "", value: "", valuedOn: today };
}

const METHOD_OPTIONS = METHODS.map((entry) => ({ value: entry, label: labelOf(METHOD_LABELS, entry) }));

function isMethod(value: string): value is ValuationMethod {
  return METHODS.some((entry) => entry === value);
}

/** The problems of a filled form; empty when it may be sent. */
export function checkAssetForm(form: AssetForm): AssetProblems {
  const problems: AssetProblems = {};
  const value = form.value.trim();
  if (value !== "" && (!isDecimalString(value) || amountSign(value) < 0)) {
    problems.value = "Enter a value of zero or more.";
  }
  if (value !== "" && !isIsoDate(form.valuedOn)) {
    problems.valuedOn = "Enter a valid date.";
  }
  return problems;
}

/** `POST /assets` for the account; a blank value sends no first valuation. */
export function assetCreateBody(form: AssetForm, account: { id: string; currency: string }): AssetCreate {
  const value = form.value.trim();
  const description = form.description.trim();
  return {
    account_id: account.id,
    valuation_method: form.method,
    ...(description === "" ? {} : { description }),
    ...(value === "" ? {} : { value: { amount: value, currency: account.currency }, valued_on: form.valuedOn }),
  };
}

export function AssetFields({
  form,
  problems,
  currency,
  onChange,
}: {
  form: AssetForm;
  problems: AssetProblems;
  currency: string;
  onChange: <K extends keyof AssetForm>(key: K, value: AssetForm[K]) => void;
}) {
  return (
    <>
      <Field label="How you value it">
        <Select
          value={form.method}
          onChange={(event) => {
            if (isMethod(event.target.value)) {
              onChange("method", event.target.value);
            }
          }}
          options={METHOD_OPTIONS}
        />
      </Field>
      <Field label="Description" hint="Optional. May hold an address; it stays out of the audit trail.">
        <Input
          value={form.description}
          onChange={(event) => {
            onChange("description", event.target.value);
          }}
        />
      </Field>
      <Field label={`First value (${currency})`} error={problems.value} hint="Optional.">
        <Input
          inputMode="decimal"
          value={form.value}
          onChange={(event) => {
            onChange("value", event.target.value);
          }}
        />
      </Field>
      <Field label="Valued on" error={problems.valuedOn}>
        <Input
          type="date"
          value={form.valuedOn}
          onChange={(event) => {
            onChange("valuedOn", event.target.value);
          }}
        />
      </Field>
    </>
  );
}

export function NewAssetDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const [form, setForm] = useState<AssetForm>(() => emptyAssetForm(todayIsoDate()));
  const [problems, setProblems] = useState<AssetProblems>({});
  const save = useAction(createAsset, { invalidate: [queryRoots.assets, queryRoots.accounts, queryRoots.netWorth], success: "Asset added" });

  const submit = async (): Promise<void> => {
    const found = checkAssetForm(form);
    setProblems(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    const done = await save.run(assetCreateBody(form, account));
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Asset details for ${accountLabel(account)}`}
      description="A property, vehicle or anything else you value yourself."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Add asset
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <AssetFields
          form={form}
          problems={problems}
          currency={account.currency}
          onChange={(key, value) => {
            setForm((current) => ({ ...current, [key]: value }));
          }}
        />
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}
