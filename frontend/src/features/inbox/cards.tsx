// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Inbox's own cards (spec section 4.1): one per subscription series that
 * waits on a person, one for the suggested categories, and the offers to
 * make a rule. A suggestion always reads as a question ("Groceries?") and
 * never as a chosen category; accepting one is a person's choice.
 */
import "./inbox.css";

import { Link } from "@tanstack/react-router";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Chip } from "../../components/Chip";
import { Spinner } from "../../components/Spinner";
import { formatDate } from "../../lib/dates";
import { amountSign, formatMoney, isDecimalString, negateAmount } from "../../lib/money";
import type { LearnedCategory } from "../categories/api";
import type { Subscription, SubscriptionDecision, SubscriptionEvent } from "../subscriptions/api";
import { merchantName, type Transaction } from "../transactions/api";
import type { SubscriptionCardEntry } from "./order";

// ---- words ---------------------------------------------------------------------

/** A cost without its sign: money out is stored negative, a price is not. */
function cost(money: { amount: string; currency: string } | null): string | null {
  if (money === null) {
    return null;
  }
  const amount = isDecimalString(money.amount) && amountSign(money.amount) < 0 ? negateAmount(money.amount) : money.amount;
  return formatMoney(amount, money.currency);
}

function dateText(iso: string): string {
  return formatDate(iso) ?? iso;
}

/** One open change of a series, as a sentence. */
export function changeText(event: Pick<SubscriptionEvent, "kind" | "occurred_on" | "amount_before" | "amount_after">): string {
  switch (event.kind) {
    case "price_changed": {
      const before = cost(event.amount_before);
      const after = cost(event.amount_after);
      const words = before === null || after === null ? "Price changed" : `Price went from ${before} to ${after}`;
      return `${words} on ${dateText(event.occurred_on)}`;
    }
    case "missed":
      return `Expected on ${dateText(event.occurred_on)}, not seen`;
    case "ended":
      return `Stopped on ${dateText(event.occurred_on)}`;
    case "new":
      return `First seen on ${dateText(event.occurred_on)}`;
    case "charged_after_end":
      return `Charged after you ended it, on ${dateText(event.occurred_on)}`;
    case "renewal_due":
      return `Renews on ${dateText(event.occurred_on)}`;
    case "trial_converted":
      return `Trial became paid on ${dateText(event.occurred_on)}`;
  }
}

const UNITS = new Map<Subscription["cadence"], [string, string]>([
  ["daily", ["daily", "days"]],
  ["weekly", ["weekly", "weeks"]],
  ["monthly", ["monthly", "months"]],
  ["quarterly", ["quarterly", "quarters"]],
  ["yearly", ["yearly", "years"]],
]);

function cadenceWords(cadence: Subscription["cadence"], every: number): string {
  const [one, many] = UNITS.get(cadence) ?? [cadence, cadence];
  return every <= 1 ? one : `every ${String(every)} ${many}`;
}

/** What PerFin thinks a suggested series is, as a question to answer. */
function guessText(kind: Subscription["kind"]): string {
  if (kind === "bill") {
    return "Might be a bill";
  }
  return kind === "subscription" ? "Might be a subscription" : "Might be recurring";
}

/** The answer that says PerFin was wrong, in the words of the series' kind. */
function dismissText(kind: Subscription["kind"]): string {
  if (kind === "bill") {
    return "Not a bill";
  }
  return kind === "subscription" ? "Not a subscription" : "Not recurring";
}

// ---- subscriptions -------------------------------------------------------------

export function SubscriptionCard({
  entry,
  canDecide,
  busy,
  onDecide,
  onSeen,
}: {
  entry: SubscriptionCardEntry;
  canDecide: boolean;
  busy: boolean;
  onDecide: (id: string, body: SubscriptionDecision) => void;
  onSeen: (ids: string[]) => void;
}) {
  const row = entry.series;
  const suggested = row?.status === "suggested";
  const price = row === null ? null : cost(row.amount);
  return (
    <Card
      className="inbox-card"
      title={entry.name}
      actions={
        <Link
          to="/plan/subscriptions/$id"
          params={{ id: entry.id }}
          className="link-action"
          aria-label={`Open ${entry.name}`}
        >
          Open
        </Link>
      }
    >
      <div className="inbox-card__body">
        {row === null ? null : (
          <p className="inbox-card__meta">
            {suggested ? <Chip tone="info">{guessText(row.kind)}</Chip> : null}
            <span className="muted">
              {price === null ? "" : `${price}, `}
              {cadenceWords(row.cadence, row.interval_count)}, {String(row.occurrences)} charges seen
            </span>
          </p>
        )}
        {entry.events.length === 0 ? null : (
          <ul className="inbox-card__changes" aria-label={`What changed about ${entry.name}`}>
            {entry.events.map((event) => (
              <li key={event.id}>{changeText(event)}</li>
            ))}
          </ul>
        )}
        {canDecide ? (
          <div className="inbox-card__actions">
            {row?.status === "suggested" ? (
              <>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  aria-label={`Confirm ${entry.name}`}
                  onClick={() => {
                    onDecide(entry.id, { decision: "confirm" });
                  }}
                >
                  Confirm
                </Button>
                <Button
                  size="sm"
                  disabled={busy}
                  aria-label={`${dismissText(row.kind)}: ${entry.name}`}
                  onClick={() => {
                    onDecide(entry.id, { decision: "dismiss" });
                  }}
                >
                  {dismissText(row.kind)}
                </Button>
              </>
            ) : null}
            {entry.events.length > 0 ? (
              <Button
                size="sm"
                disabled={busy}
                aria-label={`Got it: ${entry.name}`}
                onClick={() => {
                  onSeen(entry.events.map((event) => event.id));
                }}
              >
                Got it
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

// ---- suggested categories ------------------------------------------------------

/** One merchant's suggested rows. */
export interface MerchantGroup {
  key: string;
  name: string;
  /** Rows that carry a suggestion and no category: the only ids "Accept" sends. */
  ids: string[];
  /** The categories suggested for them, most rows first. */
  categoryIds: string[];
}

/** A row "Accept" may send: it has a suggestion, no category and no split. */
function acceptable(row: Transaction): boolean {
  return typeof row.suggested_category_id === "string" && (row.category_id ?? null) === null && row.splits.length === 0;
}

/** The suggested rows by merchant, most rows first, at most `max`. */
export function groupSuggestions(rows: readonly Transaction[], max = 5): MerchantGroup[] {
  const groups = new Map<string, { name: string; ids: string[]; categories: Map<string, number> }>();
  for (const row of rows) {
    const suggestion = row.suggested_category_id;
    if (!acceptable(row) || typeof suggestion !== "string") {
      continue;
    }
    const printed = row.raw_payee ?? "";
    const key = row.merchant_key ?? `raw:${printed}`;
    const group = groups.get(key) ?? {
      name: merchantName(row) ?? (printed === "" ? "No merchant" : printed),
      ids: [] as string[],
      categories: new Map<string, number>(),
    };
    group.ids.push(row.id);
    group.categories.set(suggestion, (group.categories.get(suggestion) ?? 0) + 1);
    groups.set(key, group);
  }
  return [...groups.entries()]
    .map(([key, group]) => ({
      key,
      name: group.name,
      ids: group.ids,
      categoryIds: [...group.categories.entries()].sort((a, b) => b[1] - a[1]).map(([id]) => id),
    }))
    .sort((a, b) => b.ids.length - a.ids.length || a.name.localeCompare(b.name))
    .slice(0, max);
}

function rowsText(n: number): string {
  return n === 1 ? "1 transaction" : `${String(n)} transactions`;
}

export function SuggestionsCard({
  total,
  rows,
  loading,
  categoryName,
  canAccept,
  busy,
  onAccept,
}: {
  /** Every suggestion the caller may see (the categorisation summary). */
  total: number;
  rows: readonly Transaction[] | undefined;
  loading: boolean;
  categoryName: (id: string) => string;
  canAccept: boolean;
  busy: boolean;
  onAccept: (group: MerchantGroup) => void;
}) {
  const groups = groupSuggestions(rows ?? []);
  const loaded = (rows ?? []).filter(acceptable).length;
  return (
    <Card
      className="inbox-card"
      title="Suggested categories"
      footer={
        <Link to="/transactions" search={{ suggested: true }} className="link-action">
          Review all in Transactions
        </Link>
      }
    >
      <div className="inbox-card__body">
        <p className="muted">
          {total > loaded && groups.length > 0
            ? `The five most common of ${rowsText(total)} with a suggestion.`
            : `${rowsText(total)} ${total === 1 ? "has" : "have"} a suggested category.`}{" "}
          Nothing changes until you accept.
        </p>
        {loading ? <Spinner label="Loading the suggestions" /> : null}
        {groups.length === 0 ? null : (
          <ul className="inbox-suggestions" aria-label="Suggestions by merchant">
            {groups.map((group) => (
              <li key={group.key} className="inbox-suggestions__row">
                <div className="inbox-suggestions__what">
                  <span className="inbox-suggestions__merchant">{group.name}</span>
                  <span className="muted">{rowsText(group.ids.length)}</span>
                </div>
                <div className="inbox-suggestions__guess">
                  {group.categoryIds.map((id) => (
                    <Chip key={id} tone="info">
                      {`${categoryName(id)}?`}
                    </Chip>
                  ))}
                </div>
                {canAccept ? (
                  <Button
                    size="sm"
                    disabled={busy}
                    aria-label={`Accept ${String(group.ids.length)} for ${group.name}`}
                    onClick={() => {
                      onAccept(group);
                    }}
                  >
                    {`Accept ${String(group.ids.length)}`}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

// ---- make it a rule ------------------------------------------------------------

export function RuleOfferCard({
  offer,
  categoryName,
  canAct,
  onHide,
}: {
  offer: LearnedCategory;
  categoryName: string;
  canAct: boolean;
  onHide: () => void;
}) {
  const example = offer.example_transaction_id;
  return (
    <Card className="inbox-card inbox-offer" title={`Always ${categoryName} for ${offer.merchant}?`}>
      <div className="inbox-card__body">
        <p className="inbox-offer__text">
          You chose {categoryName} for {offer.merchant} {String(offer.decisions)} of {String(offer.total)} times. A
          rule would do it for you from now on.
        </p>
        {canAct && example !== null ? (
          <div className="inbox-card__actions">
            <Link to="/transactions" search={{ always: example }} className="btn btn--primary btn--sm">
              {`Always use ${categoryName} for ${offer.merchant}…`}
            </Link>
            <Button size="sm" variant="ghost" aria-label={`Not now: ${offer.merchant}`} onClick={onHide}>
              Not now
            </Button>
          </div>
        ) : null}
      </div>
    </Card>
  );
}
