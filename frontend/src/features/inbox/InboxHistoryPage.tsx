// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Inbox, History (`/inbox/history`, spec section 1.3): the review items a
 * person answered or dismissed, which the Review page kept behind its "Show"
 * select. `?show=` picks Resolved (the default), Dismissed or All;
 * `?document=<id>` keeps one file's items.
 */
import { useSearch } from "@tanstack/react-router";

import { ReviewHistory } from "../review/ReviewPage";

export function InboxHistoryPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const given: unknown = new Map(Object.entries(search)).get("document");
  const documentId = typeof given === "string" && given !== "" ? given : null;
  return <ReviewHistory documentId={documentId} />;
}
