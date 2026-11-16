// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Review: one card per document that waits on a person, with its one next
 * action. `?document=<id>` shows only that document. Resolved and dismissed
 * items are listed one by one (the queue's history).
 *
 * `ReviewCardList` is the card list alone, without the header and the "Show"
 * filter: the Inbox (`/inbox`) renders it in its own order.
 */
import "../accounts/ledger.css";
import "./review.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { Fragment, useMemo, useState, type ReactNode } from "react";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import {
  listReviewCards,
  listReviewItems,
  reviewKeys,
  type ReviewCard,
  type ReviewCards,
  type ReviewDocument,
  type ReviewFilter,
} from "./api";
import {
  ClosedItem,
  DocumentCard,
  LooseCard,
  SettledCard,
  SettledLine,
} from "./DocumentCard";

export { DocumentLine, ExplanationBlock } from "./DocumentCard";

const FILTERS: readonly { value: ReviewFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "dismissed", label: "Dismissed" },
  { value: "all", label: "All" },
];

function isFilter(value: string): value is ReviewFilter {
  return FILTERS.some((filter) => filter.value === value);
}

/** A document that waited on a person while this page was open, and where its card stood. */
interface Waited {
  document: ReviewDocument;
  place: number;
}

/** `seen` with the documents of `cards` that wait on a person added (first place kept). */
function withWaiting(
  seen: ReadonlyMap<string, Waited>,
  cards: ReviewCards,
): ReadonlyMap<string, Waited> {
  const next = new Map(seen);
  cards.cards.forEach((card, place) => {
    if (card.open > 0 && !next.has(card.document.id)) {
      next.set(card.document.id, { document: card.document, place });
    }
  });
  return next.size === seen.size ? seen : next;
}

type CardSort = (cards: readonly ReviewCard[]) => ReviewCard[];

/**
 * The open cards as page entries: each document's card (or, once its last
 * waiting item was answered, the line that says where its rows went), the
 * documents that left the list while the page was open, and the loose items.
 * `documentId` keeps one document's card; `sort` orders the cards.
 */
function useCardEntries(
  documentId: string | null,
  sort: CardSort | undefined,
  enabled: boolean,
) {
  const cards = useQuery({
    queryKey: reviewKeys.cards(),
    queryFn: () => listReviewCards(),
    enabled,
  });
  const sorted = useMemo<ReviewCards | undefined>(
    () =>
      cards.data === undefined || sort === undefined
        ? cards.data
        : { ...cards.data, cards: sort(cards.data.cards) },
    [cards.data, sort],
  );
  // A card whose last waiting item was answered leaves the list when it
  // reloads. The page remembers the documents that waited, so the card can
  // stay and say where the rows went until the page is left.
  const [seen, setSeen] = useState<ReadonlyMap<string, Waited>>(new Map());
  const [from, setFrom] = useState<ReviewCards | undefined>(undefined);
  if (sorted !== from) {
    setFrom(sorted);
    if (sorted !== undefined) {
      setSeen(withWaiting(seen, sorted));
    }
  }

  const wanted = (id: string): boolean => documentId === null || documentId === id;
  const entries: Entry[] = [];
  if (enabled && sorted !== undefined) {
    const listed = new Set<string>();
    for (const card of sorted.cards) {
      listed.add(card.document.id);
      if (!wanted(card.document.id)) {
        continue;
      }
      entries.push({
        key: card.action_item_id,
        node: (
          <div className="review-entry">
            {card.open === 0 && seen.has(card.document.id) ? (
              <SettledLine document={card.document} />
            ) : null}
            <DocumentCard card={card} />
          </div>
        ),
      });
    }
    const settled = [...seen.values()]
      .filter((entry) => !listed.has(entry.document.id) && wanted(entry.document.id))
      .sort((a, b) => a.place - b.place);
    for (const entry of settled) {
      entries.splice(Math.min(entry.place, entries.length), 0, {
        key: `settled-${entry.document.id}`,
        node: <SettledCard document={entry.document} />,
      });
    }
    if (documentId === null) {
      for (const item of sorted.loose) {
        entries.push({ key: item.id, node: <LooseCard item={item} /> });
      }
    }
  }
  return { cards, entries };
}

/** One entry of the card list. */
interface Entry {
  key: string;
  node: ReactNode;
}

/**
 * The open cards without the page header and the "Show" filter, for the
 * Inbox: a list named `label`, one item per card, followed by the caller's
 * own items (`after`, already list items). Nothing is rendered when the list
 * would be empty. It shares the cards query with the caller, which shows the
 * loading and error states.
 */
export function ReviewCardList({
  documentId,
  sort,
  label,
  after = [],
}: {
  documentId: string | null;
  sort?: CardSort;
  label: string;
  after?: readonly ReactNode[];
}) {
  const { entries } = useCardEntries(documentId, sort, true);
  if (entries.length === 0 && after.length === 0) {
    return null;
  }
  return (
    <ul className="review-list" aria-label={label}>
      {entries.map((entry) => (
        <li key={entry.key} className="review-list__item">
          {entry.node}
        </li>
      ))}
      {after}
    </ul>
  );
}

export function ReviewPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const given: unknown = new Map(Object.entries(search)).get("document");
  const documentId = typeof given === "string" && given !== "" ? given : null;
  const [filter, setFilter] = useState<ReviewFilter>("open");
  const showCards = filter === "open" || filter === "all";
  const { cards, entries } = useCardEntries(documentId, undefined, showCards);
  const items = useQuery({
    queryKey: reviewKeys.list(filter, documentId),
    queryFn: () => listReviewItems(filter, documentId),
    enabled: filter !== "open",
  });

  const shown: ReactNode[] = entries.map((entry) => (
    <Fragment key={entry.key}>{entry.node}</Fragment>
  ));
  if (filter !== "open" && items.data !== undefined) {
    for (const item of items.data) {
      if (item.state !== "open") {
        shown.push(<ClosedItem key={item.id} item={item} />);
      }
    }
  }
  const pending =
    (showCards && cards.isPending) || (filter !== "open" && items.isPending);
  const failed = showCards && cards.isError ? cards.error : items.error;

  return (
    <div className="ledger-stack">
      <PageHeader
        title="Review"
        actions={
          <Field label="Show">
            <Select
              options={FILTERS}
              value={filter}
              onChange={(event) => {
                if (isFilter(event.target.value)) {
                  setFilter(event.target.value);
                }
              }}
            />
          </Field>
        }
      />
      {documentId === null ? null : (
        <p className="review-scope">
          Showing one file.{" "}
          <Link to="/inbox/history" className="link-action">
            Show every file
          </Link>
        </p>
      )}
      {pending ? <Spinner label="Loading review items" /> : null}
      {failed === null ? null : (
        <p role="alert" className="ledger-error">
          {errorMessage(failed)}
        </p>
      )}
      {!pending && failed === null && shown.length === 0 ? (
        <Card>
          <EmptyState
            title="Nothing to review"
            description="Items from imports that need a decision show up here."
          />
        </Card>
      ) : null}
      {shown}
    </div>
  );
}
