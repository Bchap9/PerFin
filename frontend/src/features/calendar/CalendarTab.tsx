// SPDX-License-Identifier: AGPL-3.0-or-later
import "../networth/wealth.css";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate } from "../../lib/dates";
import { nameLookup, useAccounts } from "../planning/queries";
import { QueryState } from "../planning/PlanningPage";
import { getCalendar, recurringKeys, type CalendarDay } from "./api";

/** Only the days something is due; the running balance is the day's end balance. */
export function eventDays(days: readonly CalendarDay[]): CalendarDay[] {
  return days.filter((day) => day.events.length > 0);
}

export function CalendarTab() {
  const [days, setDays] = useState(30);
  const query = useQuery({ queryKey: recurringKeys.calendar(days), queryFn: () => getCalendar(days) });
  const accounts = useAccounts();
  const accountName = nameLookup(accounts.data);
  const data = query.data;

  const columns: TableColumn<CalendarDay>[] = [
    { id: "on", header: "Date", rowHeader: true, cell: (day) => formatDate(day.on, { style: "full" }) ?? day.on },
    {
      id: "events",
      header: "What is due",
      cell: (day) => (
        <ul className="planning-list">
          {day.events.map((event) => (
            <li key={event.schedule_id} className="planning-list__item">
              <span>
                {event.name} <span className="muted">({accountName(event.account_id)}, {event.kind === "income" ? "income" : "bill"})</span>
              </span>
              <MoneyText amount={event.amount.amount} currency={event.amount.currency} tone="signed" signDisplay="exceptZero" />
            </li>
          ))}
        </ul>
      ),
    },
    { id: "net", header: "Day total", align: "end", cell: (day) => <MoneyText amount={day.net.amount} currency={day.net.currency} tone="signed" signDisplay="exceptZero" /> },
    { id: "balance", header: "Expected balance", align: "end", cell: (day) => <MoneyText amount={day.balance.amount} currency={day.balance.currency} /> },
  ];

  return (
    <div className="planning-stack">
      <Field label="Look ahead">
        <Select value={String(days)} onChange={(event) => { setDays(Number(event.target.value)); }} options={[30, 60, 90, 180, 365].map((value) => ({ value: String(value), label: `${String(value)} days` }))} />
      </Field>
      <QueryState loading={query.isPending} error={query.error} label="the calendar">
        {data === undefined ? null : (
          <>
            <Card title={`${formatDate(data.date_from) ?? data.date_from} to ${formatDate(data.date_to) ?? data.date_to}`} headingLevel={2}>
              <dl className="wealth-figures">
                <div>
                  <dt>Cash now</dt>
                  <dd><MoneyText amount={data.opening_balance.amount} currency={data.opening_balance.currency} /></dd>
                </div>
                <div>
                  <dt>Income expected</dt>
                  <dd><MoneyText amount={data.income.amount} currency={data.income.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Bills expected</dt>
                  <dd><MoneyText amount={data.bills.amount} currency={data.bills.currency} tone="none" /></dd>
                </div>
                <div>
                  <dt>Net</dt>
                  <dd><MoneyText amount={data.net.amount} currency={data.net.currency} tone="signed" signDisplay="exceptZero" /></dd>
                </div>
              </dl>
              <p className="muted">Based on your active recurring items at the middle of their expected range. Cash covers checking, savings and cash accounts.</p>
              {data.unconverted.length > 0 ? <p role="status" className="muted">Some items have no exchange rate and are left out of the totals.</p> : null}
            </Card>
            {eventDays(data.days).length === 0 ? (
              <EmptyState title="Nothing due" description="Add recurring items, or let PerFin find them from your transactions." />
            ) : (
              <Table caption="Upcoming days" columns={columns} rows={eventDays(data.days)} rowKey={(day) => day.on} maxHeight="none" />
            )}
          </>
        )}
      </QueryState>
    </div>
  );
}
