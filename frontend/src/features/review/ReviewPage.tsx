// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Review: one card per document that waits on a person, with its one next
 * action. `?document=<id>` shows only that document. Resolved and dismissed
 * items are listed one by one (the queue's history).
 */
import "../accounts/ledger.css";
import "./review.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import {
  listReviewCards,
  listReviewItems,
  reviewKeys,
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

export function ReviewPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const given: unknown = new Map(Object.entries(search)).get("document");
  const documentId = typeof given === "string" && given !== "" ? given : null;
  const [filter, setFilter] = useState<ReviewFilter>("open");
  const cards = useQuery({
    queryKey: reviewKeys.cards(),
    queryFn: () => listReviewCards(),
    enabled: filter === "open" || filter === "all",
  });
  const items = useQuery({
    queryKey: reviewKeys.list(filter, documentId),
    queryFn: () => listReviewItems(filter, documentId),
    enabled: filter !== "open",
  });
  // A card whose last waiting item was answered leaves the list when it
  // reloads. The page remembers the documents that waited, so the card can
  // stay and say where the rows went until the page is left.
  const [seen, setSeen] = useState<ReadonlyMap<string, Waited>>(new Map());
  const [from, setFrom] = useState<ReviewCards | undefined>(undefined);
  if (cards.data !== from) {
    setFrom(cards.data);
    if (cards.data !== undefined) {
      setSeen(withWaiting(seen, cards.data));
    }
  }

  const wanted = (id: string): boolean => documentId === null || documentId === id;
  const showCards = filter === "open" || filter === "all";
  const shown: ReactNode[] = [];
  if (showCards && cards.data !== undefined) {
    const listed = new Set<string>();
    for (const card of cards.data.cards) {
      listed.add(card.document.id);
      if (!wanted(card.document.id)) {
        continue;
      }
      shown.push(
        <div key={card.action_item_id} className="review-entry">
          {card.open === 0 && seen.has(card.document.id) ? (
            <SettledLine document={card.document} />
          ) : null}
          <DocumentCard card={card} />
        </div>,
      );
    }
    const settled = [...seen.values()]
      .filter((entry) => !listed.has(entry.document.id) && wanted(entry.document.id))
      .sort((a, b) => a.place - b.place);
    for (const entry of settled) {
      shown.splice(
        Math.min(entry.place, shown.length),
        0,
        <SettledCard key={`settled-${entry.document.id}`} document={entry.document} />,
      );
    }
    if (documentId === null) {
      for (const item of cards.data.loose) {
        shown.push(<LooseCard key={item.id} item={item} />);
      }
    }
  }
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
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Review</h1>
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
      </div>
      {documentId === null ? null : (
        <p className="review-scope">
          Showing one file.{" "}
          <Link to="/review" className="link-action">
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
    </section>
  );
}
