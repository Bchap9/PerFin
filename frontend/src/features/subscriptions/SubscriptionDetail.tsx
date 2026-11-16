// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One subscription or bill up close: what it is, what it costs, when it is
 * expected next, the charges linked to it, how its price moved and what else
 * changed. A dialog over the list on wide screens; on narrow ones a page under
 * Plan, with the breadcrumbs "Plan / Subscriptions / <name>" as the way back.
 *
 * `SeriesFacts`, `Charges`, `PriceHistory` and `Changes` are fed by props.
 */
import "./subscriptions.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState, type ReactNode } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { MoneyText } from "../../components/MoneyText";
import { compareIsoDates, formatDate } from "../../lib/dates";
import { useMediaQuery } from "../../lib/useMediaQuery";
import { useAuthSession } from "../auth/session";
import { ScheduleDialog } from "../calendar/RecurringTab";
import { PlanningPage, QueryState } from "../planning/PlanningPage";
import { useAccounts } from "../planning/queries";
import { useRouteParam } from "../planning/useRouteParam";
import { getSubscription, subscriptionKeys, type Subscription, type SubscriptionCharge, type SubscriptionEvent } from "./api";
import { NARROW_QUERY, useKindSearch } from "./SubscriptionsPage";
import { cadenceText, datedEventText, magnitude, priceLine, sourceText, statusText, unconfirmedNote } from "./text";

type Level = 2 | 3;

function Cost({ money }: { money: { amount: string; currency: string } }) {
  return <MoneyText amount={magnitude(money.amount)} currency={money.currency} tone="none" />;
}

function dateText(iso: string): string {
  return formatDate(iso) ?? iso;
}

function newestFirst<T>(rows: readonly T[], on: (row: T) => string): T[] {
  return [...rows].sort((a, b) => compareIsoDates(on(b), on(a)));
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="subs-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** What the series is and what it costs; the status says who set it. */
export function SeriesFacts({ subscription, accountName }: { subscription: Subscription; accountName: string }) {
  return (
    <div role="group" aria-label={`About ${subscription.name}`}>
      <dl className="subs-facts">
        {subscription.merchant === null ? null : <Fact label="Merchant">{subscription.merchant}</Fact>}
        {accountName === "" ? null : <Fact label="Account">{accountName}</Fact>}
        <Fact label="Status">{`${statusText(subscription.status, subscription.kind)}, ${sourceText(subscription.status_source)}`}</Fact>
        <Fact label="Repeats">{cadenceText(subscription.cadence, subscription.interval_count)}</Fact>
        <Fact label="Per month">
          <Cost money={subscription.monthly_cost} />
        </Fact>
        <Fact label="Per year">
          <Cost money={subscription.yearly_cost} />
        </Fact>
        <Fact label="Next expected charge">
          {subscription.next_due === null ? "Not known" : dateText(subscription.next_due)}
          {subscription.overdue ? (
            <>
              {" "}
              <Badge tone="warning">Not seen yet</Badge>
            </>
          ) : null}
        </Fact>
      </dl>
    </div>
  );
}

/** The charges linked to the series, newest first, and the way to them in the ledger. */
export function Charges({ id, charges, headingLevel }: { id: string; charges: readonly SubscriptionCharge[]; headingLevel: Level }) {
  const rows = useMemo(() => newestFirst(charges, (charge) => charge.posted_date), [charges]);
  return (
    <Card title="Charges" headingLevel={headingLevel}>
      {rows.length === 0 ? (
        <p className="muted">No charges are linked to this yet.</p>
      ) : (
        <table className="subs-charges">
          <caption className="visually-hidden">Charges</caption>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col" className="subs-num">
                Amount
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((charge) => (
              <tr key={charge.transaction_id}>
                <td>{dateText(charge.posted_date)}</td>
                <td className="subs-num">
                  <Cost money={charge.amount} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="subs-detail__link">
        <Link to="/transactions" search={{ subscription: id }}>
          See in transactions
        </Link>
      </p>
    </Card>
  );
}

function Lines({ lines, empty }: { lines: readonly { id: string; text: string }[]; empty: string }) {
  return lines.length === 0 ? (
    <p className="muted">{empty}</p>
  ) : (
    <ul className="subs-list">
      {lines.map((line) => (
        <li key={line.id}>{line.text}</li>
      ))}
    </ul>
  );
}

/** Every price change of the series as "<date>: <a> to <b>", newest first; one seen on a single charge says so. */
export function PriceHistory({
  events,
  priceChange,
  headingLevel,
}: {
  events: readonly SubscriptionEvent[];
  priceChange: Subscription["price_change"];
  headingLevel: Level;
}) {
  const lines = newestFirst(
    events.filter((event) => event.kind === "price_changed"),
    (event) => event.occurred_on,
  ).map((event) => ({ id: event.id, text: `${priceLine(event)}${unconfirmedNote(event, priceChange)}` }));
  return (
    <Card title="Price history" headingLevel={headingLevel}>
      <Lines lines={lines} empty="The price has not changed." />
    </Card>
  );
}

/** Everything else that happened to the series, newest first. */
export function Changes({ events, headingLevel }: { events: readonly SubscriptionEvent[]; headingLevel: Level }) {
  const lines = newestFirst(
    events.filter((event) => event.kind !== "price_changed"),
    (event) => event.occurred_on,
  ).map((event) => ({ id: event.id, text: datedEventText(event) }));
  return (
    <Card title="Changes" headingLevel={headingLevel}>
      <Lines lines={lines} empty="Nothing else has changed." />
    </Card>
  );
}

/** The series named by the route, as a dialog over the list or as a page. */
export function SubscriptionDetail() {
  const id = useRouteParam("id");
  const narrow = useMediaQuery(NARROW_QUERY);
  const kindSearch = useKindSearch();
  const navigate = useNavigate();
  const query = useQuery({ queryKey: subscriptionKeys.detail(id), queryFn: () => getSubscription(id), enabled: id !== "" });
  const accounts = useAccounts();
  const { session } = useAuthSession();
  const [editing, setEditing] = useState(false);

  const subscription = query.data?.subscription;
  const account = (accounts.data ?? []).find((candidate) => candidate.id === subscription?.account_id);
  const role = session?.household?.role;
  // A member changes only series on accounts they may edit; the server checks again.
  const mayEdit = subscription !== undefined && role !== undefined && role !== "viewer" && account?.can_edit !== false;
  // Under the dialog's h2, or under the page's h2 inside Plan.
  const headingLevel: Level = 3;
  const title = subscription?.name ?? "Subscription";

  const body = (
    <QueryState loading={query.isPending} error={query.error} label="this series">
      {query.data === undefined ? null : (
        <div className="planning-stack">
          <SeriesFacts subscription={query.data.subscription} accountName={account === undefined ? "" : (account.nickname ?? account.name)} />
          <Charges id={query.data.subscription.id} charges={query.data.charges} headingLevel={headingLevel} />
          <PriceHistory events={query.data.events} priceChange={query.data.subscription.price_change} headingLevel={headingLevel} />
          <Changes events={query.data.events} headingLevel={headingLevel} />
        </div>
      )}
    </QueryState>
  );
  const edit = mayEdit ? <Button onClick={() => { setEditing(true); }}>Edit</Button> : undefined;
  // The form is a dialog of its own, beside this one and not inside it.
  const form = editing && subscription !== undefined ? <ScheduleDialog editing={subscription} onClose={() => { setEditing(false); }} /> : null;

  if (narrow) {
    return (
      <>
        <PlanningPage
          title={title}
          actions={edit}
          breadcrumbs={[{ to: "/plan", label: "Plan" }, { to: kindSearch.kind === "bills" ? "/plan/subscriptions?kind=bills" : "/plan/subscriptions", label: "Subscriptions" }, { label: title }]}
        >
          {body}
        </PlanningPage>
        {form}
      </>
    );
  }
  return (
    <>
      <Dialog open size="lg" title={title} footer={edit} onClose={() => void navigate({ to: "/plan/subscriptions", search: kindSearch })}>
        {body}
      </Dialog>
      {form}
    </>
  );
}
