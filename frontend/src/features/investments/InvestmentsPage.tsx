// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Accounts > Investments (`/accounts/investments`): holdings, performance,
 * activity and prices across the investment accounts. The tab is in `?tab=`
 * and the account filter in `?account=`, so a view can be bookmarked.
 */
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Tabs } from "../../components/Tabs";
import { useSearchParam, useTabParam } from "../../lib/urlState";
import { useHouseholdCurrency } from "../planning/currency";
import { PlanningPage } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import { HoldingsTab } from "./HoldingsTab";
import { PerformanceTab } from "./PerformanceTab";
import { PricesTab } from "./PricesTab";
import { investmentAccounts, TransactionsTab } from "./TransactionsTab";

export const INVESTMENT_TABS = ["holdings", "performance", "activity", "prices"] as const;
export type InvestmentTab = (typeof INVESTMENT_TABS)[number];

function isInvestmentTab(id: string): id is InvestmentTab {
  return INVESTMENT_TABS.some((tab) => tab === id);
}

export function InvestmentsPage() {
  const accounts = useAccounts();
  const [tab, setTab] = useTabParam(INVESTMENT_TABS, "holdings");
  const [chosen, setAccount] = useSearchParam("account", "");
  const currency = useHouseholdCurrency() ?? "USD";
  const choices = investmentAccounts(accounts.data);
  const options = choices.map((entry) => ({ value: entry.id, label: entry.nickname ?? entry.name }));
  // An account that is not an investment account (or no longer visible) filters nothing.
  const account = accounts.data === undefined || choices.some((entry) => entry.id === chosen) ? chosen : "";
  return (
    <PlanningPage title="Investments" titleHidden>
      <Tabs
        label="Investments"
        aside={
          <Field label="Account">
            <Select value={account} placeholder="All investment accounts" onChange={(event) => { setAccount(event.target.value); }} options={options} />
          </Field>
        }
        selectedId={tab}
        onSelect={(id) => {
          if (isInvestmentTab(id)) {
            setTab(id);
          }
        }}
        tabs={[
          { id: "holdings", label: "Holdings", content: <HoldingsTab account={account} /> },
          { id: "performance", label: "Performance", content: <PerformanceTab account={account} /> },
          { id: "activity", label: "Activity", content: <TransactionsTab account={account} /> },
          { id: "prices", label: "Prices", content: <PricesTab currency={currency} /> },
        ]}
      />
    </PlanningPage>
  );
}
