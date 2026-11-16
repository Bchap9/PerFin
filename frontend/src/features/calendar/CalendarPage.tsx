// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Plan > Upcoming: the cash-flow calendar and the recurring items list. The
 * tab is in the address (`?tab=recurring`; the calendar is the default and
 * is left out), so a view can be bookmarked and shared. The page's title,
 * "Upcoming", is for screen readers only: the Plan section's Upcoming tab
 * already shows it. The calendar's look-ahead sits on the tab row
 * (as the investment account filter does) and only while the calendar shows.
 */
import { useState } from "react";

import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Tabs } from "../../components/Tabs";
import { useTabParam } from "../../lib/urlState";
import { PlanningPage } from "../planning/PlanningPage";
import { CalendarTab, LOOK_AHEAD_DAYS } from "./CalendarTab";
import { RecurringTab } from "./RecurringTab";

const TABS = ["calendar", "recurring"] as const;
type UpcomingTab = (typeof TABS)[number];

export function CalendarPage() {
  const [tab, setTab] = useTabParam<UpcomingTab>(TABS, "calendar");
  const [days, setDays] = useState<number>(30);
  return (
    <PlanningPage title="Upcoming" titleHidden>
      <Tabs
        label="Upcoming"
        {...(tab === "calendar"
          ? {
              aside: (
                <Field label="Look ahead">
                  <Select
                    value={String(days)}
                    onChange={(event) => { setDays(Number(event.target.value)); }}
                    options={LOOK_AHEAD_DAYS.map((value) => ({ value: String(value), label: `Next ${String(value)} days` }))}
                  />
                </Field>
              ),
            }
          : {})}
        selectedId={tab}
        onSelect={(id) => { setTab(id === "recurring" ? "recurring" : "calendar"); }}
        tabs={[
          { id: "calendar", label: "Cash-flow calendar", content: <CalendarTab days={days} /> },
          { id: "recurring", label: "Recurring items", content: <RecurringTab /> },
        ]}
      />
    </PlanningPage>
  );
}
