// SPDX-License-Identifier: AGPL-3.0-or-later
/** The loan terms inputs shared by the "New loan" and "Edit loan" dialogs. */
import type { ReactNode } from "react";

import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { LENDER_MAX, type LoanForm, type LoanProblems } from "./loanForm";

const RATE_TYPE_OPTIONS = [
  { value: "fixed", label: "Fixed" },
  { value: "variable", label: "Variable" },
];

export function LoanTermsFields({
  form,
  problems,
  currency,
  onChange,
  paymentHint = "Principal and interest. Leave blank for the level payment.",
  showPaymentDay = false,
}: {
  form: LoanForm;
  problems: LoanProblems;
  currency: string;
  onChange: <K extends keyof LoanForm>(key: K, value: LoanForm[K]) => void;
  paymentHint?: ReactNode;
  /** The due day of the month (an existing loan always has one). */
  showPaymentDay?: boolean;
}) {
  return (
    <>
      <Field label={`Original amount (${currency})`} error={problems.principal} required>
        <Input inputMode="decimal" value={form.principal} onChange={(event) => { onChange("principal", event.target.value); }} />
      </Field>
      <Field label="Yearly rate (%)" error={problems.rate} required>
        <Input inputMode="decimal" value={form.rate} onChange={(event) => { onChange("rate", event.target.value); }} />
      </Field>
      <Field label="Rate type">
        <Select value={form.rateType} onChange={(event) => { onChange("rateType", event.target.value === "variable" ? "variable" : "fixed"); }} options={RATE_TYPE_OPTIONS} />
      </Field>
      <Field label="Term (months)" error={problems.term} required>
        <Input inputMode="numeric" value={form.term} onChange={(event) => { onChange("term", event.target.value); }} />
      </Field>
      <Field label="Origination date" error={problems.origination} required>
        <Input type="date" value={form.origination} onChange={(event) => { onChange("origination", event.target.value); }} />
      </Field>
      <Field label={`Monthly payment (${currency})`} error={problems.payment} hint={paymentHint}>
        <Input inputMode="decimal" value={form.payment} onChange={(event) => { onChange("payment", event.target.value); }} />
      </Field>
      {showPaymentDay ? (
        <Field label="Payment day of the month" error={problems.paymentDay} required hint="In a shorter month, days 29 to 31 fall on its last day.">
          <Input inputMode="numeric" value={form.paymentDay} onChange={(event) => { onChange("paymentDay", event.target.value); }} />
        </Field>
      ) : null}
      <Field label={`Escrow per month (${currency})`} error={problems.escrow}>
        <Input inputMode="decimal" value={form.escrow} onChange={(event) => { onChange("escrow", event.target.value); }} />
      </Field>
      <Field label="Lender">
        <Input value={form.lender} maxLength={LENDER_MAX} onChange={(event) => { onChange("lender", event.target.value); }} />
      </Field>
    </>
  );
}
