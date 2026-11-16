// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * After a person corrected a category in the grid: one line that offers to
 * make it the merchant's rule, when that would do something. The server
 * counts (`POST /rules/merchant/preview`); the line shows only when the row
 * can teach, no rule covers the merchant yet, and at least one other
 * transaction has no category or carries a different guess. It never blocks,
 * and "Not now" keeps it away for that merchant until the page is reloaded.
 */
import "./transactions.css";

import { Button } from "../../components/Button";
import { merchantName, useMerchantRulePreview, type Category, type Transaction } from "./api";

/** A correction that may become a rule: the row as it was corrected and the category chosen. */
export interface TeachOffer {
  transaction: Transaction;
  categoryId: string;
}

/** Merchant keys the person answered "Not now" for. In memory only: it lasts as long as the page. */
const dismissed = new Set<string>();

export function teachDismissed(merchantKey: string): boolean {
  return dismissed.has(merchantKey);
}

export function dismissTeach(merchantKey: string): void {
  dismissed.add(merchantKey);
}

/** For tests: a page load starts with nothing dismissed. */
export function forgetTeachDismissals(): void {
  dismissed.clear();
}

const others = (count: number, one: string, many: string): string => (count === 1 ? `1 other transaction ${one}` : `${String(count)} other transactions ${many}`);

export interface TeachPromptProps {
  offer: TeachOffer;
  categories: readonly Category[];
  /** Open the dialog for this row and category. */
  onAlways: () => void;
  /** "Not now". */
  onDismiss: () => void;
}

export function TeachPrompt({ offer, categories, onAlways, onDismiss }: TeachPromptProps) {
  const preview = useMerchantRulePreview(offer.transaction.id, offer.categoryId);
  const category = categories.find((candidate) => candidate.id === offer.categoryId)?.name;
  const data = preview.data;
  // Silent unless there is something true and useful to offer.
  if (data === undefined || category === undefined) {
    return null;
  }
  if (!data.teachable || data.existing_rule_id !== null || data.shadowed_by !== null || data.uncategorised + data.guessed < 1) {
    return null;
  }
  const merchant = data.merchant ?? merchantName(offer.transaction) ?? "this merchant";
  const what =
    data.uncategorised > 0 ? others(data.uncategorised, "has no category yet", "have no category yet") : others(data.guessed, "has a different guess", "have a different guess");
  return (
    <div className="tx-notice" role="group" aria-label="Make it a rule?">
      <p role="status" className="tx-notice__text">
        Use {category} for {merchant} from now on? {what}.
      </p>
      <span className="tx-notice__actions">
        <Button size="sm" onClick={onAlways}>
          Always…
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          Not now
        </Button>
      </span>
    </div>
  );
}
