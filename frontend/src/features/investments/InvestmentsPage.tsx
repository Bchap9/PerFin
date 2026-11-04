// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Tabs } from "../../components/Tabs";
import { useHouseholdCurrency } from "../planning/currency";
import { PlanningPage } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import { HoldingsTab } from "./HoldingsTab";
import { PerformanceTab } from "./PerformanceTab";
import { PricesTab } from "./PricesTab";
import { investmentAccounts, TransactionsTab } from "./TransactionsTab";

export function InvestmentsPage() {
  const accounts = useAccounts();
  const [account, setAccount] = useState("");
  const currency = useHouseholdCurrency() ?? "USD";
  const options = investmentAccounts(accounts.data).map((entry) => ({ value: entry.id, label: entry.nickname ?? entry.name }));
  return (
    <PlanningPage
      title="Investments"
      actions={
        <Field label="Account">
          <Select value={account} placeholder="All investment accounts" onChange={(event) => { setAccount(event.target.value); }} options={options} />
        </Field>
      }
    >
      <Tabs
        label="Investments"
        tabs={[
          { id: "holdings", label: "Holdings", content: <HoldingsTab account={account} /> },
          { id: "performance", label: "Performance", content: <PerformanceTab account={account} /> },
          { id: "transactions", label: "Transactions", content: <TransactionsTab account={account} /> },
          { id: "prices", label: "Prices", content: <PricesTab currency={currency} /> },
        ]}
      />
    </PlanningPage>
  );
}
