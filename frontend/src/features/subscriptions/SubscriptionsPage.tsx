// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Subscriptions and bills: what they cost, what needs a look, what is due in
 * the next 30 days, the active ones and, folded away, the ended and dismissed
 * ones. Every button is a person's decision (`POST /subscriptions/{id}/decision`),
 * their word on which of the two a series is (`PATCH /recurring/{id}` with the
 * kind alone, which leaves the status as it is) or "seen" for a change;
 * detection never undoes any of them.
 *
 * The sections are separate components fed by props, so the next stage can
 * rehouse them without touching the data layer. A series' name opens it up
 * close (`/subscriptions/:id`, `SubscriptionDetail`).
 */
import "./subscriptions.css";

import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useChildMatches } from "@tanstack/react-router";
import { useId, useMemo, useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { MoneyText } from "../../components/MoneyText";
import { Tabs } from "../../components/Tabs";
import { addDays, compareIsoDates, formatDate, todayIsoDate } from "../../lib/dates";
import { compareAmounts, isDecimalString } from "../../lib/money";
import { useAuthSession } from "../auth/session";
import { ErrorLine, PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAccounts, type Account } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { updateSchedule } from "../calendar/api";
import {
  decide,
  listOpenEvents,
  listSubscriptions,
  markEventsSeen,
  SUBSCRIPTION_FAMILIES,
  subscriptionKeys,
  type Subscription,
  type SubscriptionDecision,
  type SubscriptionEvent,
  type SubscriptionTotals,
} from "./api";
import { cadenceText, eventText, magnitude, statusText, unconfirmedNote } from "./text";
import { useWide } from "./useWide";

type TabKind = "subscription" | "bill";

const UPCOMING_DAYS = 30;

interface Words {
  one: string;
  many: string;
  dismiss: string;
  switchKind: string;
  other: TabKind;
  empty: string;
}

const WORDS = new Map<TabKind, Words>([
  [
    "subscription",
    {
      one: "subscription",
      many: "subscriptions",
      dismiss: "Not a subscription",
      switchKind: "This is a bill",
      other: "bill",
      empty: "No subscriptions found yet. They appear after a few statements are imported.",
    },
  ],
  [
    "bill",
    {
      one: "bill",
      many: "bills",
      dismiss: "Not a bill",
      switchKind: "This is a subscription",
      other: "subscription",
      empty: "No bills found yet. They appear after a few statements are imported.",
    },
  ],
]);

function wordsFor(kind: TabKind): Words {
  const words = WORDS.get(kind);
  if (words === undefined) {
    throw new Error("unknown kind");
  }
  return words;
}

type Decide = (subscription: Subscription, body: SubscriptionDecision) => void;
/** A person's word on which of the two a series is; its status stays as it is. */
type SetKind = (subscription: Subscription, kind: TabKind) => void;

function Cost({ money }: { money: { amount: string; currency: string } }) {
  return <MoneyText amount={magnitude(money.amount)} currency={money.currency} tone="none" />;
}

/** The name of a series, as the way to it up close. */
function SeriesName({ row }: { row: Subscription }) {
  return (
    <Link to="/subscriptions/$id" params={{ id: row.id }} className="subs-name">
      {row.name}
    </Link>
  );
}

function dateText(iso: string | null): string {
  return iso === null ? "Not known" : (formatDate(iso) ?? iso);
}

const UNLISTED = "one that is not listed here";

/**
 * The two figures and the count for one kind; series without a rate are named.
 * `totals.unconverted` holds ids: `nameOf` turns one into the name of its series.
 */
export function CostHeader({
  kind,
  totals,
  asOf,
  nameOf,
}: {
  kind: TabKind;
  totals: SubscriptionTotals;
  asOf: string;
  nameOf: (id: string) => string | undefined;
}) {
  const words = wordsFor(kind);
  return (
    <div role="group" aria-label={`Cost of ${words.many}`} className="subs-cost">
      <dl className="subs-figures">
        <div className="subs-figure">
          <dt>Per month</dt>
          <dd>
            <Cost money={totals.monthly} />
          </dd>
        </div>
        <div className="subs-figure">
          <dt>Per year</dt>
          <dd>
            <Cost money={totals.yearly} />
          </dd>
        </div>
      </dl>
      <p className="subs-cost__count">{`${String(totals.count)} ${totals.count === 1 ? words.one : words.many}`}</p>
      {totals.unconverted.map((id) => (
        <p key={id} className="muted subs-cost__note">{`Not included: ${nameOf(id) ?? UNLISTED}`}</p>
      ))}
      <p className="muted subs-cost__note">{`From statements up to ${dateText(asOf)}`}</p>
    </div>
  );
}

/** What changed, in words; a change whose words are not at hand (or could not be read) is still said. */
function changeLines(subscription: Subscription, events: readonly SubscriptionEvent[], eventsSettled: boolean): string[] {
  if (events.length > 0) {
    return events.map((event) => `${eventText(event)}${unconfirmedNote(event, subscription.price_change)}`);
  }
  return subscription.open_events > 0 && eventsSettled ? ["Something changed"] : [];
}

/** Suggested series and series with a change nobody has looked at. */
export function NeedsLook({
  kind,
  rows,
  eventsFor,
  eventsSettled,
  canDecide,
  onDecide,
  onSeen,
  busy,
}: {
  kind: TabKind;
  rows: readonly Subscription[];
  eventsFor: (id: string) => readonly SubscriptionEvent[];
  eventsSettled: boolean;
  canDecide: (subscription: Subscription) => boolean;
  onDecide: Decide;
  onSeen: (ids: string[]) => void;
  busy: boolean;
}) {
  const words = wordsFor(kind);
  if (rows.length === 0) {
    return null;
  }
  return (
    <Card title="Needs a look" headingLevel={2}>
      <ul className="subs-list">
        {rows.map((row) => {
          const events = eventsFor(row.id);
          const suggested = row.status === "suggested";
          const allowed = canDecide(row);
          return (
            <li key={row.id} className="subs-look">
              <div className="subs-look__main">
                <SeriesName row={row} />
                <span className="muted">
                  <Cost money={row.amount} />, {cadenceText(row.cadence, row.interval_count).toLowerCase()}
                </span>
                {suggested ? <Badge tone="info">{statusText(row.status, row.kind)}</Badge> : null}
                {changeLines(row, events, eventsSettled).map((line, index) => (
                  <p key={`${String(index)}:${line}`} className="subs-look__change">
                    {line}
                  </p>
                ))}
              </div>
              {allowed ? (
                <div className="subs-actions">
                  {suggested ? (
                    <>
                      <Button size="sm" variant="primary" disabled={busy} aria-label={`Confirm ${row.name}`} onClick={() => { onDecide(row, { decision: "confirm" }); }}>
                        Confirm
                      </Button>
                      <Button size="sm" disabled={busy} aria-label={`${words.dismiss}: ${row.name}`} onClick={() => { onDecide(row, { decision: "dismiss" }); }}>
                        {words.dismiss}
                      </Button>
                    </>
                  ) : null}
                  {events.length > 0 ? (
                    <Button size="sm" disabled={busy} aria-label={`Got it: ${row.name}`} onClick={() => { onSeen(events.map((event) => event.id)); }}>
                      Got it
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/** What is expected in the next 30 days: date, name, amount. */
export function Upcoming({ rows }: { rows: readonly Subscription[] }) {
  return (
    <Card title="Next 30 days" headingLevel={2}>
      {rows.length === 0 ? (
        <p className="muted">Nothing is expected in the next 30 days.</p>
      ) : (
        <ul className="subs-list">
          {rows.map((row) => (
            <li key={row.id} className="subs-upcoming">
              <span className="subs-upcoming__date">{dateText(row.next_due)}</span>
              <SeriesName row={row} />
              <span className="subs-upcoming__amount">
                <Cost money={row.amount} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function RowMenu({ kind, row, onDecide, onKind, busy }: { kind: TabKind; row: Subscription; onDecide: Decide; onKind: SetKind; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const words = wordsFor(kind);
  const paused = row.status === "paused";
  const choose = (body: SubscriptionDecision): void => {
    setOpen(false);
    onDecide(row, body);
  };
  return (
    <div className="subs-menu">
      <Button
        size="sm"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={`More for ${row.name}`}
        onClick={() => { setOpen((current) => !current); }}
      >
        More
      </Button>
      {open ? (
        <div id={panelId} role="group" aria-label={`Actions for ${row.name}`} className="subs-menu__items">
          <Button size="sm" disabled={busy} onClick={() => { choose({ decision: paused ? "resume" : "pause" }); }}>
            {paused ? "Resume" : "Pause"}
          </Button>
          <Button size="sm" disabled={busy} onClick={() => { choose({ decision: "end" }); }}>
            Ended
          </Button>
          {/* Which of the two it is: the kind alone, so a series PerFin found is not confirmed by it. */}
          <Button size="sm" disabled={busy} onClick={() => { setOpen(false); onKind(row, words.other); }}>
            {words.switchKind}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function yearlyCost(row: Subscription): string {
  const amount = magnitude(row.yearly_cost.amount);
  return isDecimalString(amount) ? amount : "0";
}

/** Active, confirmed and paused series; sorted by yearly cost, dearest first by default. */
export function ActiveTable({
  kind,
  rows,
  accountName,
  canDecide,
  onDecide,
  onKind,
  busy,
}: {
  kind: TabKind;
  rows: readonly Subscription[];
  accountName: (id: string) => string;
  canDecide: (subscription: Subscription) => boolean;
  onDecide: Decide;
  onKind: SetKind;
  busy: boolean;
}) {
  const [ascending, setAscending] = useState(false);
  const sorted = useMemo(() => {
    const direction = ascending ? 1 : -1;
    return [...rows].sort((a, b) => direction * compareAmounts(yearlyCost(a), yearlyCost(b)) || a.name.localeCompare(b.name));
  }, [rows, ascending]);
  const anyActions = rows.some(canDecide);
  return (
    <Card title="Active" headingLevel={2}>
      {rows.length === 0 ? (
        <p className="muted">Nothing active.</p>
      ) : (
        <table className="subs-table">
          <caption className="visually-hidden">Active</caption>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Merchant</th>
              <th scope="col">Account</th>
              <th scope="col" className="subs-num">
                Amount
              </th>
              <th scope="col">Repeats</th>
              <th scope="col">Next charge</th>
              <th scope="col" className="subs-num" aria-sort={ascending ? "ascending" : "descending"}>
                <button type="button" className="subs-sort" onClick={() => { setAscending((current) => !current); }}>
                  Per year
                  <span aria-hidden="true">{ascending ? " ▲" : " ▼"}</span>
                  <span className="visually-hidden">{ascending ? ", lowest first" : ", highest first"}</span>
                </button>
              </th>
              {anyActions ? (
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.id}>
                <th scope="row" className="subs-col-name">
                  <SeriesName row={row} /> <Badge tone={row.status === "paused" ? "warning" : row.status === "confirmed" ? "success" : "info"}>{statusText(row.status, row.kind)}</Badge>
                </th>
                <td data-label="Merchant" className="subs-col-rest">
                  {row.merchant ?? ""}
                </td>
                <td data-label="Account" className="subs-col-rest">
                  {accountName(row.account_id)}
                </td>
                <td data-label="Amount" className="subs-num subs-col-amount">
                  <Cost money={row.amount} />
                </td>
                <td data-label="Repeats" className="subs-col-rest">
                  {cadenceText(row.cadence, row.interval_count)}
                </td>
                <td data-label="Next charge" className="subs-col-next">
                  {dateText(row.next_due)}
                  {row.overdue ? (
                    <>
                      {" "}
                      <Badge tone="warning">Not seen yet</Badge>
                    </>
                  ) : null}
                </td>
                <td data-label="Per year" className="subs-num subs-col-rest">
                  <Cost money={row.yearly_cost} />
                </td>
                {anyActions ? (
                  <td className="subs-col-actions">{canDecide(row) ? <RowMenu kind={kind} row={row} onDecide={onDecide} onKind={onKind} busy={busy} /> : null}</td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

/** Ended and dismissed series, folded; a person can reopen one. */
export function Folded({
  rows,
  canDecide,
  onDecide,
  busy,
}: {
  rows: readonly Subscription[];
  canDecide: (subscription: Subscription) => boolean;
  onDecide: Decide;
  busy: boolean;
}) {
  if (rows.length === 0) {
    return null;
  }
  return (
    <details className="subs-folded" aria-label="Ended and dismissed">
      <summary>{`Ended and dismissed (${String(rows.length)})`}</summary>
      <ul className="subs-list">
        {rows.map((row) => (
          <li key={row.id} className="subs-look">
            <div className="subs-look__main">
              <SeriesName row={row} />
              <Badge>{statusText(row.status, row.kind)}</Badge>
              <span className="muted">
                <Cost money={row.amount} />, {cadenceText(row.cadence, row.interval_count).toLowerCase()}
              </span>
            </div>
            {canDecide(row) ? (
              <div className="subs-actions">
                <Button size="sm" disabled={busy} aria-label={`Reopen ${row.name}`} onClick={() => { onDecide(row, { decision: "reopen" }); }}>
                  Reopen
                </Button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

function accountLookup(accounts: readonly Account[] | undefined): Map<string, Account> {
  return new Map((accounts ?? []).map((account) => [account.id, account]));
}

function isUpcoming(row: Subscription, today: string): boolean {
  if ((row.status !== "active" && row.status !== "confirmed") || row.next_due === null) {
    return false;
  }
  return compareIsoDates(row.next_due, today) >= 0 && compareIsoDates(row.next_due, addDays(today, UPCOMING_DAYS)) <= 0;
}

/** The list with its figures and tabs. */
export function SubscriptionsList() {
  const list = useQuery({ queryKey: subscriptionKeys.list, queryFn: listSubscriptions });
  const openEvents = useQuery({ queryKey: subscriptionKeys.openEvents, queryFn: listOpenEvents });
  const accounts = useAccounts();
  const { session } = useAuthSession();
  const [tab, setTab] = useState<TabKind>("subscription");
  const decision = useAction((id: string, body: SubscriptionDecision) => decide(id, body), { invalidate: SUBSCRIPTION_FAMILIES });
  const seen = useAction(markEventsSeen, { invalidate: SUBSCRIPTION_FAMILIES });
  const kindChange = useAction((id: string, kind: TabKind) => updateSchedule(id, { kind }), { invalidate: SUBSCRIPTION_FAMILIES });

  const role = session?.household?.role;
  const mayDecide = role !== undefined && role !== "viewer";
  const byAccount = useMemo(() => accountLookup(accounts.data), [accounts.data]);
  const eventsBySeries = useMemo(() => {
    const grouped = new Map<string, SubscriptionEvent[]>();
    for (const event of openEvents.data ?? []) {
      grouped.set(event.schedule_id, [...(grouped.get(event.schedule_id) ?? []), event]);
    }
    return grouped;
  }, [openEvents.data]);

  // A member decides only about series on accounts they may edit; the server checks again.
  const canDecide = (row: Subscription): boolean => mayDecide && byAccount.get(row.account_id)?.can_edit !== false;
  const accountName = (id: string): string => {
    const account = byAccount.get(id);
    return account === undefined ? "" : (account.nickname ?? account.name);
  };
  const onDecide: Decide = (row, body) => {
    void decision.run(row.id, body);
  };
  const onSeen = (ids: string[]): void => {
    void seen.run(ids);
  };
  const onKind: SetKind = (row, kind) => {
    void kindChange.run(row.id, kind);
  };
  const busy = decision.busy || seen.busy || kindChange.busy;
  const today = todayIsoDate();
  const data = list.data;
  const nameOf = (id: string): string | undefined => data?.items.find((row) => row.id === id)?.name;

  const panel = (kind: TabKind) => {
    const rows = (data?.items ?? []).filter((row) => row.kind === kind);
    if (rows.length === 0) {
      return <p className="subs-empty">{wordsFor(kind).empty}</p>;
    }
    const upcoming = rows
      .filter((row) => isUpcoming(row, today))
      .sort((a, b) => compareIsoDates(a.next_due ?? "", b.next_due ?? "") || a.name.localeCompare(b.name));
    return (
      <div className="planning-stack">
        <NeedsLook
          kind={kind}
          rows={rows.filter((row) => row.status === "suggested" || row.open_events > 0)}
          eventsFor={(id) => eventsBySeries.get(id) ?? []}
          eventsSettled={!openEvents.isPending}
          canDecide={canDecide}
          onDecide={onDecide}
          onSeen={onSeen}
          busy={busy}
        />
        <Upcoming rows={upcoming} />
        <ActiveTable
          kind={kind}
          rows={rows.filter((row) => row.status === "active" || row.status === "confirmed" || row.status === "paused")}
          accountName={accountName}
          canDecide={canDecide}
          onDecide={onDecide}
          onKind={onKind}
          busy={busy}
        />
        <Folded rows={rows.filter((row) => row.status === "ended" || row.status === "dismissed")} canDecide={canDecide} onDecide={onDecide} busy={busy} />
      </div>
    );
  };

  return (
    <PlanningPage title="Subscriptions">
      <QueryState loading={list.isPending} error={list.error} label="subscriptions">
        {data === undefined ? null : (
          <>
            <CostHeader kind={tab} totals={tab === "bill" ? data.bills : data.subscriptions} asOf={data.as_of} nameOf={nameOf} />
            <ErrorLine message={decision.error ?? seen.error ?? kindChange.error} />
            <ErrorLine message={openEvents.isError ? "Could not load what changed." : null} />
            <Tabs
              label="Subscriptions and bills"
              selectedId={tab}
              onSelect={(id) => { setTab(id === "bill" ? "bill" : "subscription"); }}
              tabs={[
                { id: "subscription", label: "Subscriptions", content: panel("subscription") },
                { id: "bill", label: "Bills", content: panel("bill") },
              ]}
            />
          </>
        )}
      </QueryState>
    </PlanningPage>
  );
}

/**
 * The view in the shell. One series opens over the list on a wide screen; on
 * a narrow one it takes the place of the list, which comes back with it gone.
 */
export function SubscriptionsPage() {
  const wide = useWide();
  const detailOpen = useChildMatches().length > 0;
  return (
    <>
      {detailOpen && !wide ? null : <SubscriptionsList />}
      <Outlet />
    </>
  );
}
