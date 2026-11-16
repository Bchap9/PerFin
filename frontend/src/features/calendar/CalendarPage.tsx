// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Plan > Upcoming: the cash-flow calendar and the recurring items list. The
 * tab is in the address (`?tab=recurring`; the calendar is the default and
 * is left out), so a view can be bookmarked and shared.
 *
 * The page keeps the title "Calendar" under the "Upcoming" tab for now:
 * `app/redirects.test.tsx` (Start-owned) expects that heading at
 * `/plan/upcoming`. Renaming it is left to the integration task.
 */
import { Tabs } from "../../components/Tabs";
import { useTabParam } from "../../lib/urlState";
import { PlanningPage } from "../planning/PlanningPage";
import { CalendarTab } from "./CalendarTab";
import { RecurringTab } from "./RecurringTab";

const TABS = ["calendar", "recurring"] as const;
type UpcomingTab = (typeof TABS)[number];

export function CalendarPage() {
  const [tab, setTab] = useTabParam<UpcomingTab>(TABS, "calendar");
  return (
    <PlanningPage title="Calendar">
      <Tabs
        label="Upcoming"
        selectedId={tab}
        onSelect={(id) => { setTab(id === "recurring" ? "recurring" : "calendar"); }}
        tabs={[
          { id: "calendar", label: "Cash-flow calendar", content: <CalendarTab /> },
          { id: "recurring", label: "Recurring items", content: <RecurringTab /> },
        ]}
      />
    </PlanningPage>
  );
}
