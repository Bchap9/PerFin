// SPDX-License-Identifier: AGPL-3.0-or-later
import { Tabs } from "../../components/Tabs";
import { PlanningPage } from "../planning/PlanningPage";
import { CalendarTab } from "./CalendarTab";
import { RecurringTab } from "./RecurringTab";

export function CalendarPage() {
  return (
    <PlanningPage title="Calendar">
      <Tabs
        label="Calendar"
        tabs={[
          { id: "calendar", label: "Cash-flow calendar", content: <CalendarTab /> },
          { id: "recurring", label: "Recurring and subscriptions", content: <RecurringTab /> },
        ]}
      />
    </PlanningPage>
  );
}
