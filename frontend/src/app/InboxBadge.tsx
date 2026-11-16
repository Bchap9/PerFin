// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The Inbox count on the rail: the number of Inbox cards, read as "3 things
 * need you". Nothing at zero, while loading or on an error (never a wrong
 * zero). Not a live region, so a new count does not interrupt.
 */
import { useInboxSummary } from "./inboxSummary";

export function InboxBadge() {
  const summary = useInboxSummary();
  const cards = summary.data?.cards ?? 0;
  if (summary.isError || cards <= 0) {
    return null;
  }
  // The name reads "Inbox, 3 things need you" in one piece; the visible pill is decoration.
  return (
    <>
      <span className="visually-hidden">{`, ${String(cards)} things need you`}</span>
      <span className="badge badge--count" aria-hidden="true">
        {cards}
      </span>
    </>
  );
}
