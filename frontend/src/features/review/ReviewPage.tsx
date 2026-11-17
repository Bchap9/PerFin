// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The review queue on the Inbox.
 *
 * - `ReviewCardList`: one card per document that waits on a person, with its
 *   one next action, then the loose items. Inbox, Needs you (`/inbox`)
 *   renders it in its own order; `?document=<id>` keeps one document.
 * - `ReviewHistory`: the items a person resolved or dismissed, one by one.
 *   Inbox, History (`/inbox/history`) renders it.
 */
import "../accounts/ledger.css";
import "./review.css";

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { PageHeader } from "../../components/PageHeader";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { useSearchParam } from "../../lib/urlState";
import {
  listReviewCards,
  listReviewItems,
  reviewKeys,
  type ReviewCard,
  type ReviewCards,
  type ReviewDocument,
} from "./api";
import {
  ClosedItem,
  DocumentCard,
  LooseCard,
  SettledCard,
  SettledLine,
} from "./DocumentCard";
import { itemAnchor } from "../inbox/order";

export { DocumentLine, ExplanationBlock } from "./DocumentCard";

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
function useCardEntries(documentId: string | null, sort: CardSort | undefined): Entry[] {
  const cards = useQuery({
    queryKey: reviewKeys.cards(),
    queryFn: () => listReviewCards(),
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
  if (sorted !== undefined) {
    const listed = new Set<string>();
    for (const card of sorted.cards) {
      listed.add(card.document.id);
      if (!wanted(card.document.id)) {
        continue;
      }
      entries.push({
        key: card.action_item_id,
        anchor: documentAnchor(card.document.id),
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
        anchor: documentAnchor(entry.document.id),
        node: <SettledCard document={entry.document} />,
      });
    }
    if (documentId === null) {
      for (const item of sorted.loose) {
        entries.push({ key: item.id, anchor: itemAnchor(item.id), node: <LooseCard item={item} /> });
      }
    }
  }
  return entries;
}

/** One entry of the card list. */
interface Entry {
  key: string;
  /**
   * The card's anchor, so an upload row or a Home row can point at it
   * (`#inbox-document-<id>`, `#inbox-item-<id>`).
   */
  anchor: string;
  node: ReactNode;
}

/** The anchor of a document's card on the Inbox, which an upload row links to. */
export function documentAnchor(documentId: string): string {
  return `inbox-document-${documentId}`;
}

/**
 * The open cards without the page header and the "Show" filter, for the
 * Inbox: a list named `label`, one item per card, followed by the caller's
 * own items (`after`, already list items). When the list would be empty it
 * renders `empty` instead, so an empty state never sits beside a card that
 * was just answered. It shares the cards query with the caller, which shows
 * the loading and error states.
 */
export function ReviewCardList({
  documentId,
  sort,
  label,
  after = [],
  empty = null,
}: {
  documentId: string | null;
  sort?: CardSort;
  label: string;
  after?: readonly ReactNode[];
  empty?: ReactNode;
}) {
  const entries = useCardEntries(documentId, sort);
  // An answer replaces the card's question (or the card), and the pressed
  // button with it: focus then moves to the card's item, not the page body.
  const last = useRef("");
  const keys = entries.map((entry) => entry.key).join();
  useEffect(() => {
    if (document.activeElement === document.body && last.current !== "") {
      document.getElementById(last.current)?.focus();
    }
  }, [keys]);
  if (entries.length === 0 && after.length === 0) {
    return empty;
  }
  return (
    <ul
      className="review-list"
      aria-label={label}
      onFocus={(event) => {
        last.current = event.target.closest(".review-list__item")?.id ?? "";
      }}
    >
      {entries.map((entry) => (
        <li key={entry.key} id={entry.anchor} tabIndex={-1} className="review-list__item">
          {entry.node}
        </li>
      ))}
      {after}
    </ul>
  );
}

/** The answered filters of the history, with the one shown by default first. */
const HISTORY_FILTERS: readonly { value: "resolved" | "dismissed" | "all"; label: string }[] = [
  { value: "resolved", label: "Resolved" },
  { value: "dismissed", label: "Dismissed" },
  { value: "all", label: "All" },
];

type HistoryFilter = (typeof HISTORY_FILTERS)[number]["value"];

function historyFilter(value: string): HistoryFilter {
  return HISTORY_FILTERS.find((filter) => filter.value === value)?.value ?? "resolved";
}

/**
 * The queue's history (Inbox, History): the items a person resolved or
 * dismissed, one by one, filtered by `?show=` (Resolved by default, Dismissed,
 * All). Open items are never listed here: they are cards on Needs you.
 * `documentId` keeps one document's items.
 */
export function ReviewHistory({ documentId }: { documentId: string | null }) {
  const [shown, setShown] = useSearchParam("show", "resolved");
  const filter = historyFilter(shown);
  const items = useQuery({
    queryKey: reviewKeys.list(filter, documentId),
    queryFn: () => listReviewItems(filter, documentId),
  });
  const answered = (items.data ?? []).filter((item) => item.state !== "open");

  return (
    <div className="ledger-stack">
      <PageHeader
        title="Answered and dismissed"
        actions={
          <Field label="Show">
            <Select
              options={HISTORY_FILTERS}
              value={filter}
              onChange={(event) => {
                setShown(historyFilter(event.target.value));
              }}
            />
          </Field>
        }
      />
      {documentId === null ? null : (
        <p className="review-scope">
          Showing one file.{" "}
          <Link to="/inbox/history" search={filter === "resolved" ? {} : { show: filter }} className="link-action">
            Show every file
          </Link>
        </p>
      )}
      {items.isPending ? <Spinner label="Loading answered items" /> : null}
      {items.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(items.error)}
        </p>
      ) : null}
      {items.isSuccess && answered.length === 0 ? (
        <Card>
          <EmptyState
            title="Nothing here yet"
            description="Questions you answer or dismiss on Needs you are kept here."
          />
        </Card>
      ) : null}
      {answered.length === 0 ? null : (
        <ul className="review-list" aria-label="Answered items">
          {answered.map((item) => (
            <li key={item.id} className="review-list__item">
              <ClosedItem item={item} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
