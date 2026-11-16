// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Inbox, Needs you (`/inbox`, spec section 4): everything that waits on a
 * person, one card each, in the order that matters (`order.ts`).
 *
 * 1. The upload zone: an Upload button, a drop zone and one row per file sent.
 * 2. The document cards, then the loose items (`GET /review/cards`).
 * 3. One card per subscription series that is suggested or has open changes.
 * 4. The suggested categories, and under them the rows with no category.
 * 5. Up to three offers to make a rule (not questions, so not counted).
 * 6. Imported recently: where each finished file went.
 *
 * The cards that need an answer are one list, as many as
 * `GET /inbox/summary` counts. `?document=<id>` shows only that document's
 * card. An answered card stays until the page is left, saying what happened.
 */
import "./inbox.css";

import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Spinner } from "../../components/Spinner";
import { queryRoots } from "../../lib/queryKeys";
import { useInboxSummary } from "../../app/inboxSummary";
import { useAccounts } from "../accounts/api";
import { useAuthSession } from "../auth/session";
import { useAction } from "../planning/useAction";
import { listReviewCards, reviewKeys } from "../review/api";
import { ReviewCardList } from "../review/ReviewPage";
import {
  decide,
  listOpenEvents,
  markEventsSeen,
  SUBSCRIPTION_FAMILIES,
  subscriptionKeys,
  type SubscriptionDecision,
} from "../subscriptions/api";
import { bulkAction, useCategories, useCategorisationSummary } from "../transactions/api";
import { inboxPageKeys, listRuleOffers, listSuggestedRows, listSuggestedSeries } from "./api";
import { RuleOfferCard, SubscriptionCard, SuggestionsCard, type MerchantGroup } from "./cards";
import { orderCards, subscriptionCards } from "./order";
import { RecentImports } from "./RecentImports";
import { UploadZone } from "./UploadZone";

/** At most this many "Make it a rule" offers are shown. */
const OFFERS_MAX = 3;

const HIDDEN_KEY = "perfin.inbox.hiddenOffers";

/** Offers set aside with "Not now" in this browser session (kept in `sessionStorage`). */
function readHidden(): ReadonlySet<string> {
  try {
    const raw = window.sessionStorage.getItem(HIDDEN_KEY);
    const parsed: unknown = raw === null ? [] : JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : []);
  } catch {
    return new Set();
  }
}

function writeHidden(ids: ReadonlySet<string>): void {
  try {
    window.sessionStorage.setItem(HIDDEN_KEY, JSON.stringify([...ids]));
  } catch {
    // Storage unavailable: the offer stays hidden while the page is open.
  }
}

/** What the ledger's change by "Accept" moves, and the Inbox count. */
const ACCEPT_FAMILIES = [
  queryRoots.transactions,
  queryRoots.accounts,
  queryRoots.budgets,
  queryRoots.goals,
  queryRoots.reports,
  queryRoots.dashboard,
  queryRoots.inbox,
] as const;

const DECISION_FAMILIES = [...SUBSCRIPTION_FAMILIES, queryRoots.inbox] as const;

/** One line saying what could not be loaded, with Retry. */
function Failed({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <p role="alert" className="inbox-error">
      Could not load {what}.{" "}
      <Button size="sm" variant="ghost" onClick={onRetry} aria-label={`Retry loading ${what}`}>
        Retry
      </Button>
    </p>
  );
}

function documentParam(search: Record<string, unknown>): string | null {
  const given: unknown = new Map(Object.entries(search)).get("document");
  return typeof given === "string" && given !== "" ? given : null;
}

export function InboxPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const documentId = documentParam(search);
  const everything = documentId === null;
  const { session } = useAuthSession();
  const role = session?.household?.role;
  const mayWrite = role !== undefined && role !== "viewer";
  const accounts = useAccounts();
  const summary = useInboxSummary();

  const cards = useQuery({ queryKey: reviewKeys.cards(), queryFn: () => listReviewCards() });
  const suggestedSeries = useQuery({
    queryKey: inboxPageKeys.suggestedSeries,
    queryFn: listSuggestedSeries,
    enabled: everything,
  });
  const openEvents = useQuery({ queryKey: subscriptionKeys.openEvents, queryFn: listOpenEvents, enabled: everything });
  const categorisation = useCategorisationSummary();
  const suggestionCount = categorisation.data?.suggestions ?? 0;
  const suggestedRows = useQuery({
    queryKey: inboxPageKeys.suggestedRows,
    queryFn: listSuggestedRows,
    enabled: everything && suggestionCount > 0,
  });
  const offers = useQuery({ queryKey: inboxPageKeys.ruleOffers, queryFn: listRuleOffers, enabled: everything });
  const categories = useCategories();

  const decision = useAction((id: string, body: SubscriptionDecision) => decide(id, body), {
    invalidate: DECISION_FAMILIES,
  });
  const seen = useAction(markEventsSeen, { invalidate: DECISION_FAMILIES });
  const accept = useAction(
    (group: MerchantGroup) => bulkAction({ action: "accept_suggestions", ids: group.ids }),
    {
      invalidate: ACCEPT_FAMILIES,
      success: (result) =>
        result.changed === 1 ? "1 transaction categorised" : `${String(result.changed)} transactions categorised`,
    },
  );
  const [hidden, setHidden] = useState<ReadonlySet<string>>(readHidden);

  const series = useMemo(
    () => (everything ? subscriptionCards(suggestedSeries.data ?? [], openEvents.data ?? []) : []),
    [everything, suggestedSeries.data, openEvents.data],
  );
  const accountById = useMemo(
    () => new Map((accounts.data ?? []).map((account) => [account.id, account])),
    [accounts.data],
  );
  const categoryNames = useMemo(
    () => new Map((categories.data ?? []).map((category) => [category.id, category.name])),
    [categories.data],
  );
  const categoryName = (id: string): string => categoryNames.get(id) ?? "A category";

  const shownOffers = everything
    ? (offers.data ?? []).filter((offer) => !hidden.has(offer.id)).slice(0, OFFERS_MAX)
    : [];
  const loose = cards.data?.loose ?? [];
  const looseMore = everything ? Math.max(0, (summary.data?.loose ?? 0) - loose.length) : 0;
  const hasDocumentCards = (cards.data?.cards ?? []).some(
    (card) => documentId === null || card.document.id === documentId,
  );
  const showSuggestions = everything && suggestionCount > 0;
  const uncategorised = everything ? (categorisation.data?.uncategorised ?? 0) : 0;
  const allClear =
    everything &&
    cards.isSuccess &&
    cards.data.cards.length === 0 &&
    cards.data.loose.length === 0 &&
    suggestedSeries.isSuccess &&
    openEvents.isSuccess &&
    series.length === 0 &&
    categorisation.isSuccess &&
    suggestionCount === 0;

  const busy = decision.busy || seen.busy;
  const actionError = decision.error ?? seen.error ?? accept.error;

  const after = [
    ...(looseMore > 0
      ? [
          <li key="loose-more" className="inbox-more">
            {looseMore === 1 ? "1 more not shown" : `${String(looseMore)} more not shown`}
          </li>,
        ]
      : []),
    ...series.map((entry) => {
      const account = entry.series === null ? undefined : accountById.get(entry.series.account_id);
      return (
        <li key={`series-${entry.id}`} className="review-list__item">
          <SubscriptionCard
            entry={entry}
            canDecide={mayWrite && account?.can_edit !== false}
            busy={busy}
            onDecide={(id, body) => {
              void decision.run(id, body);
            }}
            onSeen={(ids) => {
              void seen.run(ids);
            }}
          />
        </li>
      );
    }),
    ...(showSuggestions
      ? [
          <li key="suggestions" className="review-list__item">
            <SuggestionsCard
              total={suggestionCount}
              rows={suggestedRows.data}
              loading={suggestedRows.isPending}
              categoryName={categoryName}
              canAccept={mayWrite}
              busy={accept.busy}
              onAccept={(group) => {
                void accept.run(group);
              }}
            />
          </li>,
        ]
      : []),
  ];

  return (
    <div className="inbox">
      <UploadZone />
      <div className="inbox__needs">
        {everything ? null : (
          <p className="review-scope">
            Showing one file.{" "}
            <Link to="/inbox" className="link-action">
              Show everything
            </Link>
          </p>
        )}
        {cards.isPending ? <Spinner label="Loading what needs you" /> : null}
        {cards.isError ? <Failed what="the documents that need you" onRetry={() => void cards.refetch()} /> : null}
        {suggestedSeries.isError || openEvents.isError ? (
          <Failed
            what="the subscriptions that need you"
            onRetry={() => {
              void suggestedSeries.refetch();
              void openEvents.refetch();
            }}
          />
        ) : null}
        {everything && categorisation.isError ? (
          <Failed what="the suggested categories" onRetry={() => void categorisation.refetch()} />
        ) : null}
        {suggestedRows.isError ? (
          <Failed what="the suggested transactions" onRetry={() => void suggestedRows.refetch()} />
        ) : null}
        {offers.isError ? <Failed what="the rules you could make" onRetry={() => void offers.refetch()} /> : null}
        {actionError === null ? null : (
          <p role="alert" className="inbox-error">
            {actionError}
          </p>
        )}
        {cards.isSuccess && !everything && !hasDocumentCards ? (
          <p className="muted">Nothing waits on this file.</p>
        ) : null}

        <ReviewCardList
          documentId={documentId}
          sort={orderCards}
          label="Waiting on you"
          after={after}
          empty={
            allClear ? (
              <Card className="inbox-clear" title="All clear">
                <p className="muted">Every document you added is imported. Drop new statements here any time.</p>
              </Card>
            ) : null
          }
        />

        {uncategorised > 0 ? (
          <p className="inbox-uncategorised">
            <Link to="/transactions" search={{ category: "none" }} className="link-action">
              {uncategorised === 1 ? "1 has no category" : `${String(uncategorised)} have no category`}
            </Link>
          </p>
        ) : null}

        {shownOffers.length === 0 ? null : (
          <ul className="inbox-list" aria-label="Rules you could make">
            {shownOffers.map((offer) => (
              <li key={offer.id} className="review-list__item">
                <RuleOfferCard
                  offer={offer}
                  categoryName={categoryName(offer.category_id)}
                  canAct={mayWrite}
                  onHide={() => {
                    const next = new Set(hidden);
                    next.add(offer.id);
                    writeHidden(next);
                    setHidden(next);
                  }}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
      {everything ? <RecentImports /> : null}
    </div>
  );
}
