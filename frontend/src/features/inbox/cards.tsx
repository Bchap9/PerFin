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
import type { LearnedCategory } from "../categories/api";
import type { Subscription, SubscriptionDecision } from "../subscriptions/api";
import { merchantName, type Transaction } from "../transactions/api";
import type { SubscriptionCardEntry } from "./order";
import { changeText, cost, guessText } from "./words";

// ---- words ---------------------------------------------------------------------

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

/** Printed text with fewer letters and digits than this says too little to group rows by. */
const MIN_GROUP_CHARACTERS = 3;

function groupable(printed: string): boolean {
  return (printed.match(/[\p{L}\p{N}]/gu)?.length ?? 0) >= MIN_GROUP_CHARACTERS;
}

/**
 * The suggested rows by merchant, most rows first, at most `max`. A row with
 * no merchant groups by its printed text ("raw:"), unless that text has
 * fewer than three letters and digits ("#1", "-", none): such a row stands
 * alone, so one Accept never covers unrelated rows.
 */
export function groupSuggestions(rows: readonly Transaction[], max = 5): MerchantGroup[] {
  const groups = new Map<string, { name: string; ids: string[]; categories: Map<string, number> }>();
  for (const row of rows) {
    const suggestion = row.suggested_category_id;
    if (!acceptable(row) || typeof suggestion !== "string") {
      continue;
    }
    const printed = row.raw_payee ?? "";
    const key = row.merchant_key ?? (groupable(printed) ? `raw:${printed}` : `row:${row.id}`);
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
