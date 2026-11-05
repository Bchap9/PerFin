// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { isCurrencyCode } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { canManageMembers, useAuthSession } from "../auth/session";
import { useHousehold } from "../planning/currency";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { updateHousehold, type Household } from "./dataApi";
import { SettingsPage } from "./SettingsLayout";

function HouseholdForm({ household, canEdit }: { household: Household; canEdit: boolean }) {
  const [name, setName] = useState(household.name);
  const [currency, setCurrency] = useState(household.base_currency);
  const [problems, setProblems] = useState<Record<string, string>>({});
  const save = useAction((body: { name: string; base_currency: string }) => updateHousehold(household.id, body), {
    invalidate: [queryRoots.settings, queryRoots.session],
    success: "Household saved",
  });
  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    const code = currency.trim().toUpperCase();
    if (name.trim() === "") {
      found["name"] = "Enter a name.";
    }
    if (!isCurrencyCode(code)) {
      found["currency"] = "Use a three-letter currency code, such as USD or EUR.";
    }
    setProblems(found);
    if (Object.keys(found).length === 0) {
      await save.run({ name: name.trim(), base_currency: code });
    }
  };
  return (
    <Card title="Household" headingLevel={2}>
      <form
        className="planning-form"
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field label="Household name" error={problems["name"]}>
          <Input value={name} disabled={!canEdit} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Field
          label="Base currency"
          hint="Reports and budgets total in this currency. Existing amounts keep the currency they were recorded in."
          error={problems["currency"]}
        >
          <Input value={currency} maxLength={3} disabled={!canEdit} onChange={(event) => { setCurrency(event.target.value); }} />
        </Field>
        <ErrorLine message={save.error} />
        {canEdit ? (
          <div>
            <Button type="submit" variant="primary" loading={save.busy}>
              Save
            </Button>
          </div>
        ) : (
          <p className="muted">Only an owner or admin can change these.</p>
        )}
      </form>
    </Card>
  );
}

export function GeneralPage() {
  const { session } = useAuthSession();
  const query = useHousehold();
  return (
    <SettingsPage title="Household">
      <QueryState loading={query.isPending && session?.household != null} error={query.error} label="the household">
        {query.data === undefined ? null : <HouseholdForm key={`${query.data.id}:${query.data.name}:${query.data.base_currency}`} household={query.data} canEdit={canManageMembers(session?.household?.role)} />}
      </QueryState>
    </SettingsPage>
  );
}
