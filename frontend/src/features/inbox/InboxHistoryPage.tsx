// SPDX-License-Identifier: AGPL-3.0-or-later
// Stage 3 bridge: lane I replaces this.
import { ReviewPage } from "../review/ReviewPage";

/** Inbox, History (`/inbox/history`): today's review page, whose "Show" select reaches answered and dismissed items. */
export function InboxHistoryPage() {
  return <ReviewPage />;
}
