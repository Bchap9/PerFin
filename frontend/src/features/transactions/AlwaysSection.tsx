// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Always use <category> for <merchant>": making a merchant's rule from one
 * row, for members and above. This module holds the logic the drawer and the
 * row menu's dialog share: the server's preview of what the rule would meet
 * (how many transactions have no category, how many carry a guess that would
 * change, how many a person or another rule decided, which never change),
 * the save, and its undo.
 *
 * `AlwaysSection` is the drawer's part: one box, unticked unless the person
 * came asking for it (`?always=`). The server is asked only once it is
 * ticked. Changing the existing transactions is a second box, also unticked,
 * and Save sends it as `apply_to_existing`. A row that cannot teach, or a
 * merchant an earlier rule already decides, is explained instead, with a link
 * to that rule. `RuleSavedNotice` is what follows a save: the undo of a rule
 * this save made, and the job's progress when existing transactions change.
 */
import "./transactions.css";

import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { createMerchantRule, revertRule, type MerchantRulePreview, type MerchantRuleSaved } from "../rules/api";
import { JobProgress } from "../rules/JobProgress";
import {
  canTeach,
  invalidateLedger,
  merchantName,
  useCategories,
  useMerchantRulePreview,
  useRuleNames,
  type Category,
  type Transaction,
} from "./api";

/** A rule that was just saved, with what is needed to undo it. */
export interface SavedRule {
  transactionId: string;
  merchant: string;
  categoryName: string;
  /** Whether the existing transactions were asked to change too. */
  applied: boolean;
  /** The merchant had its rule before this save: the save made no rule, so there is none to undo. */
  existed: boolean;
  /** The name of the category the rule had before, when this save changed it. */
  previousCategoryName: string | null;
  result: MerchantRuleSaved;
}

type Reason = NonNullable<MerchantRulePreview["reason"]>;

const REASONS: ReadonlyMap<Reason, string> = new Map<Reason, string>([
  ["no_merchant", "PerFin could not tell which merchant this is, so there is nothing to make a rule from."],
  ["not_a_purchase", "This is not a purchase (it reads like a transfer, a card payment, cash or a payment to a person), so no merchant rule can be made from it."],
  ["split", "This transaction is split over several categories, so no merchant rule can be made from it."],
]);

const CANNOT_TEACH = "No merchant rule can be made from this transaction.";

/** Why a row cannot teach, from the row alone (no merchant, or split), so the server need not be asked. */
export function localReason(row: Pick<Transaction, "merchant_key" | "splits">): string | null {
  if (canTeach(row)) {
    return null;
  }
  return (row.splits.length > 0 ? REASONS.get("split") : REASONS.get("no_merchant")) ?? CANNOT_TEACH;
}

/** The counts of the preview as sentences; a count of zero is left out. */
export function previewSentences(preview: Pick<MerchantRulePreview, "uncategorised" | "guessed" | "decided" | "same_category">): string[] {
  const { uncategorised, guessed, decided, same_category: same } = preview;
  return [
    ...(uncategorised === 0 ? [] : [uncategorised === 1 ? "1 transaction without a category" : `${String(uncategorised)} transactions without a category`]),
    ...(guessed === 0 ? [] : [guessed === 1 ? "1 that PerFin guessed differently will change" : `${String(guessed)} that PerFin guessed differently will change`]),
    ...(decided === 0
      ? []
      : [
          decided === 1
            ? "1 that was chosen by hand or by another rule stays as it is"
            : `${String(decided)} that were chosen by hand or by another rule stay as they are`,
        ]),
    ...(same === 0 ? [] : [same === 1 ? "1 that is in this category already" : `${String(same)} that are in this category already`]),
  ];
}

export interface AlwaysRuleOptions {
  transaction: Transaction;
  categories: readonly Category[];
  categoryId: string;
  /** Ask the server for the preview (the drawer asks only once its box is ticked). */
  enabled: boolean;
}

/** The preview, the second box and the save of a merchant rule from one row. */
export function useAlwaysRule({ transaction, categories, categoryId, enabled }: AlwaysRuleOptions) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [applyToExisting, setApplyToExisting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A tick of the second box was for the counts of one category; another category starts unticked.
  const [askedFor, setAskedFor] = useState(categoryId);
  if (askedFor !== categoryId) {
    setAskedFor(categoryId);
    setApplyToExisting(false);
    setError(null);
  }
  const category = categories.find((candidate) => candidate.id === categoryId);
  const preview = useMerchantRulePreview(transaction.id, category === undefined ? "" : categoryId, enabled);
  const data = category === undefined || !enabled ? undefined : preview.data;
  const shadowedBy = data?.shadowed_by ?? null;
  const ruleNames = useRuleNames(shadowedBy !== null);
  const merchant = data?.merchant ?? merchantName(transaction) ?? "this merchant";
  const savable = data?.teachable === true && shadowedBy === null;
  const changeable = data === undefined ? 0 : data.uncategorised + data.guessed;

  /** Save the rule; the saved rule, or null when nothing was saved (the error is in `error`). */
  const save = async (): Promise<SavedRule | null> => {
    if (category === undefined || data === undefined || !savable) {
      return null;
    }
    // The box only counts while it is on screen.
    const applied = applyToExisting && changeable > 0;
    setBusy(true);
    setError(null);
    try {
      const result = await createMerchantRule({ transaction_id: transaction.id, category_id: category.id, apply_to_existing: applied });
      void queryClient.invalidateQueries({ queryKey: queryRoots.rules });
      invalidateLedger(queryClient);
      toast.notify({ tone: "success", title: "Rule saved" });
      const previous = result.previous_category_id ?? null;
      return {
        transactionId: transaction.id,
        merchant,
        categoryName: category.name,
        applied,
        existed: data.existing_rule_id !== null,
        previousCategoryName: previous === null ? null : (categories.find((candidate) => candidate.id === previous)?.name ?? null),
        result,
      };
    } catch (caught) {
      setError(errorMessage(caught));
      return null;
    } finally {
      setBusy(false);
    }
  };

  return { category, preview, data, shadowedBy, ruleNames, merchant, savable, changeable, applyToExisting, setApplyToExisting, busy, error, save };
}

export type AlwaysRule = ReturnType<typeof useAlwaysRule>;

/** What the server says the rule would do, the reason it cannot be made, or the earlier rule that decides the merchant. */
export function AlwaysPreview({ rule }: { rule: AlwaysRule }) {
  const { category, preview, data, shadowedBy, ruleNames, merchant, changeable, applyToExisting, setApplyToExisting } = rule;
  if (category === undefined) {
    return <p className="ledger-muted">Choose a category to see what would change.</p>;
  }
  if (preview.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(preview.error)}
      </p>
    );
  }
  if (data === undefined) {
    return <p className="ledger-muted">Counting your transactions…</p>;
  }
  if (!data.teachable) {
    return <p>{(data.reason === null ? undefined : REASONS.get(data.reason)) ?? CANNOT_TEACH}</p>;
  }
  if (shadowedBy !== null) {
    return (
      <p>
        The rule{" "}
        <Link to="/settings/rules/$ruleId" params={{ ruleId: shadowedBy }}>
          {ruleNames?.get(shadowedBy) ?? "that runs earlier"}
        </Link>{" "}
        already decides this merchant, so a rule saved here would never be reached. Change that rule instead.
      </p>
    );
  }
  const sentences = previewSentences(data);
  return (
    <>
      {data.existing_rule_id === null ? null : <p>{merchant} has a rule already. Saving changes its category.</p>}
      {sentences.length === 0 ? (
        <p className="ledger-muted">No other transaction from {merchant} is in the accounts you can edit.</p>
      ) : (
        <>
          <p>Transactions from {merchant} in the accounts you can edit:</p>
          <ul className="tx-preview__counts">
            {sentences.map((sentence) => (
              <li key={sentence}>{sentence}</li>
            ))}
          </ul>
        </>
      )}
      {changeable === 0 ? null : (
        <Checkbox
          label={changeable === 1 ? "Also change the existing 1 transaction" : `Also change the existing ${String(changeable)} transactions`}
          checked={applyToExisting}
          onChange={(event) => {
            setApplyToExisting(event.target.checked);
          }}
        />
      )}
    </>
  );
}

export interface AlwaysSectionProps {
  transaction: Transaction;
  /** The category the rule would set: the row's, or its suggestion. */
  categoryId: string;
  /** The person came asking for the rule (`?always=`): the box starts ticked. Nothing is saved before Save. */
  defaultChecked: boolean;
}

/** The drawer's "Always use <category> for <merchant>". */
export function AlwaysSection({ transaction, categoryId, defaultChecked }: AlwaysSectionProps) {
  const categories = useCategories();
  const [ticked, setTicked] = useState(defaultChecked);
  const [saved, setSaved] = useState<SavedRule | null>(null);
  const [removed, setRemoved] = useState(false);
  const reason = localReason(transaction);
  const rule = useAlwaysRule({ transaction, categories: categories.data ?? [], categoryId, enabled: ticked && reason === null });
  const merchant = merchantName(transaction) ?? "this merchant";

  return (
    <section className="tx-always" aria-label="Make it a rule">
      {reason !== null ? (
        <p className="ledger-muted">{reason}</p>
      ) : rule.category === undefined ? (
        <p className="ledger-muted">Choose a category to make it the rule for {merchant}.</p>
      ) : (
        <>
          <Checkbox
            label={`Always use ${rule.category.name} for ${merchant}`}
            checked={ticked}
            onChange={(event) => {
              setTicked(event.target.checked);
            }}
          />
          {ticked ? (
            <div aria-live="polite" className="tx-preview">
              <AlwaysPreview rule={rule} />
              {rule.savable ? (
                <span>
                  <Button
                    size="sm"
                    variant="primary"
                    loading={rule.busy}
                    onClick={() => {
                      void rule.save().then((result) => {
                        if (result !== null) {
                          setRemoved(false);
                          setSaved(result);
                          setTicked(false);
                        }
                      });
                    }}
                  >
                    {rule.data?.existing_rule_id == null ? "Save rule" : "Change the rule"}
                  </Button>
                </span>
              ) : null}
              {rule.error === null ? null : (
                <p role="alert" className="ledger-error">
                  {rule.error}
                </p>
              )}
            </div>
          ) : null}
        </>
      )}
      {saved === null ? null : (
        <RuleSavedNotice
          key={`${saved.result.rule.id}:${saved.result.rule.updated_at}`}
          saved={saved}
          onDone={() => {
            setSaved(null);
          }}
          onUndone={() => {
            setRemoved(true);
          }}
        />
      )}
      {/* The drawer is modal, over the toasts: the undo is said here too. */}
      {removed ? (
        <p role="status" className="ledger-muted">
          Rule removed. What it had categorised is without a category again, for PerFin to guess anew.
        </p>
      ) : null}
    </section>
  );
}

export interface RuleSavedNoticeProps {
  saved: SavedRule;
  /** The notice is no longer needed: hidden, or undone. */
  onDone: () => void;
  /** The rule was removed (called before `onDone`). */
  onUndone?: () => void;
}

/**
 * After a rule was saved: what it does, "Undo", and the job's progress when
 * existing transactions are being changed. Undo is offered only for a rule
 * this save made: it removes the rule and returns what it categorised to
 * undecided (`POST /rules/{id}/revert`). A rule that was there before is
 * never reverted from here, because that would delete it together with
 * everything it ever categorised. A changed rule has no undo either: the only
 * way to put its category back is to save the merchant's rule again, and the
 * server then also sets this transaction to that category as the person's
 * own choice, which is a decision nobody made. The notice says how to go back
 * by hand instead.
 */
export function RuleSavedNotice({ saved, onDone, onUndone }: RuleSavedNoticeProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { result } = saved;
  // Only a rule this save made: `replaced` alone is false both for a new rule
  // and for an existing one saved again with the category it had.
  const canUndo = !saved.existed && !result.replaced;
  const earlier = !saved.existed
    ? null
    : !result.replaced
      ? `${saved.merchant} had this rule already; it is as it was.`
      : `${saved.previousCategoryName === null ? "It had another category before" : `It was ${saved.previousCategoryName} before`}. To go back, choose that category for the rule again.`;

  const undo = async (): Promise<void> => {
    if (!canUndo) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await revertRule(result.rule.id);
      toast.notify({ tone: "success", title: "Rule removed", description: "What it had categorised is without a category again, for PerFin to guess anew." });
      void queryClient.invalidateQueries({ queryKey: queryRoots.rules });
      invalidateLedger(queryClient);
      onUndone?.();
      onDone();
    } catch (caught) {
      toast.notify({ tone: "error", title: "Could not undo the rule", description: errorMessage(caught) });
      // Also here, where focus is: a toast can sit behind a modal drawer.
      setError(`Could not undo the rule: ${errorMessage(caught)}`);
      setBusy(false);
    }
  };

  return (
    <div className="tx-notice" role="group" aria-label="Rule saved">
      <p role="status" className="tx-notice__text">
        {saved.merchant} is always {saved.categoryName} from now on.{earlier === null ? null : ` ${earlier}`}
      </p>
      {result.job_id === null ? null : (
        <div className="tx-notice__job">
          <span>Changing the existing transactions:</span>
          <JobProgress
            jobId={result.job_id}
            onFinished={() => {
              invalidateLedger(queryClient);
            }}
          />
        </div>
      )}
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
      <span className="tx-notice__actions">
        {canUndo ? (
          <Button size="sm" loading={busy} onClick={() => void undo()}>
            Undo
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={onDone}>
          Hide
        </Button>
      </span>
    </div>
  );
}
