// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Always categorise <merchant> as...": one step from a row to the merchant's
 * rule, for members and above. The merchant is the one the server recognised
 * (nothing is built from the description in the browser). Before saving, the
 * server says what the rule would meet in the ledger: how many transactions
 * have no category, how many carry a guess that would change, and how many a
 * person or another rule decided, which never change. Changing the existing
 * ones is a separate choice and not the default. A row that cannot teach, or
 * a merchant an earlier rule already decides, is explained and cannot be
 * saved. `RuleSavedNotice` is what the page shows afterwards: the undo of a
 * rule this save made, and the progress of the job when existing transactions
 * are being changed.
 */
import "./transactions.css";

import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { CategoryPicker } from "../../components/CategoryPicker";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { createMerchantRule, revertRule, type MerchantRulePreview, type MerchantRuleSaved } from "../rules/api";
import { JobProgress } from "../rules/JobProgress";
import { invalidateLedger, merchantName, useMerchantRulePreview, useRuleNames, type Category, type Transaction } from "./api";

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

export interface AlwaysCategoriseDialogProps {
  transaction: Transaction;
  categories: readonly Category[];
  /** The category to start from; the row's category or suggestion otherwise. */
  categoryId?: string | undefined;
  onClose: () => void;
  /** The rule was saved (the dialog closes itself afterwards). */
  onSaved: (saved: SavedRule) => void;
}

export function AlwaysCategoriseDialog({ transaction, categories, categoryId: startWith, onClose, onSaved }: AlwaysCategoriseDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [categoryId, setCategoryId] = useState(startWith ?? transaction.category_id ?? transaction.suggested_category_id ?? "");
  const [applyToExisting, setApplyToExisting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const category = categories.find((candidate) => candidate.id === categoryId);
  const preview = useMerchantRulePreview(transaction.id, category === undefined ? "" : categoryId);
  const data = category === undefined ? undefined : preview.data;
  const ruleNames = useRuleNames(data?.shadowed_by != null);
  const merchant = data?.merchant ?? merchantName(transaction) ?? "this merchant";
  const raw = (transaction.raw_payee ?? "").trim();
  const shadowedBy = data?.shadowed_by ?? null;
  const savable = data?.teachable === true && shadowedBy === null;
  const changeable = data === undefined ? 0 : data.uncategorised + data.guessed;
  const sentences = data === undefined ? [] : previewSentences(data);

  const save = async (): Promise<void> => {
    if (category === undefined || !savable) {
      return;
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
      onSaved({
        transactionId: transaction.id,
        merchant,
        categoryName: category.name,
        applied,
        existed: data.existing_rule_id !== null,
        previousCategoryName: previous === null ? null : (categories.find((candidate) => candidate.id === previous)?.name ?? null),
        result,
      });
      onClose();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Always categorise ${merchant}`}
      description={
        data?.existing_rule_id != null
          ? "Changes the rule this merchant has. It runs on every import from now on."
          : "Saves a rule for this merchant. It runs on every import from now on, and you can undo it."
      }
      footer={
        <>
          <Button onClick={onClose}>{savable ? "Cancel" : "Close"}</Button>
          {savable ? (
            <Button variant="primary" loading={busy} onClick={() => void save()}>
              {data.existing_rule_id === null ? "Save rule" : "Change the rule"}
            </Button>
          ) : null}
        </>
      }
    >
      <div className="tx-rule-form">
        {raw === "" ? null : (
          <p className="ledger-muted">
            On your statement: <span className="payee-cell__raw">{raw}</span>
          </p>
        )}
        <Field label="Set the category to" required>
          <CategoryPicker
            categories={categories}
            placeholder="Choose a category"
            value={categoryId}
            onChange={(event) => {
              setCategoryId(event.target.value);
              setError(null);
            }}
          />
        </Field>
        <div aria-live="polite" className="tx-preview">
          {category === undefined ? (
            <p className="ledger-muted">Choose a category to see what would change.</p>
          ) : preview.isError ? (
            <p role="alert" className="ledger-error">
              {errorMessage(preview.error)}
            </p>
          ) : data === undefined ? (
            <p className="ledger-muted">Counting your transactions…</p>
          ) : !data.teachable ? (
            <p>{(data.reason === null ? undefined : REASONS.get(data.reason)) ?? CANNOT_TEACH}</p>
          ) : shadowedBy !== null ? (
            <p>
              The rule{" "}
              <Link to="/rules/$ruleId" params={{ ruleId: shadowedBy }}>
                {ruleNames?.get(shadowedBy) ?? "that runs earlier"}
              </Link>{" "}
              already decides this merchant, so a rule saved here would never be reached. Change that rule instead.
            </p>
          ) : (
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
          )}
        </div>
        {error === null ? null : (
          <p role="alert" className="ledger-error">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}

export interface RuleSavedNoticeProps {
  saved: SavedRule;
  /** The notice is no longer needed: hidden, or undone. */
  onDone: () => void;
}

/**
 * Under the grid's toolbar after a rule was saved: what it does, "Undo", and
 * the job's progress when existing transactions are being changed. Undo is
 * offered only for a rule this save made: it removes the rule and returns
 * what it categorised to undecided (`POST /rules/{id}/revert`). A rule that
 * was there before is never reverted from here, because that would delete it
 * together with everything it ever categorised. A changed rule has no undo
 * either: the only way to put its category back is to save the merchant's
 * rule again, and the server then also sets this transaction to that category
 * as the person's own choice, which is a decision nobody made. The notice
 * says how to go back by hand instead.
 */
export function RuleSavedNotice({ saved, onDone }: RuleSavedNoticeProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
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
    try {
      await revertRule(result.rule.id);
      toast.notify({ tone: "success", title: "Rule removed", description: "What it had categorised is without a category again, for PerFin to guess anew." });
      void queryClient.invalidateQueries({ queryKey: queryRoots.rules });
      invalidateLedger(queryClient);
      onDone();
    } catch (caught) {
      toast.notify({ tone: "error", title: "Could not undo the rule", description: errorMessage(caught) });
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
