// SPDX-License-Identifier: AGPL-3.0-or-later
// Stage 3 bridge: lane I replaces this.
import { UploadPage } from "../imports/UploadPage";
import { ReviewPage } from "../review/ReviewPage";

/** Inbox, Needs you (`/inbox`): today's uploader, then today's review. `?document=` still scopes the review. */
export function InboxPage() {
  return (
    <>
      <UploadPage />
      <ReviewPage />
    </>
  );
}
