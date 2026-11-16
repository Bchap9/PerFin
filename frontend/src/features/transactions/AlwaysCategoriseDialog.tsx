// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Always categorise <merchant> as...", from the row menu: the same rule as
 * the drawer's "Always use ... for ..." (`AlwaysSection`), in a dialog that
 * lets the category be chosen first. The preview, the save and its undo are
 * `AlwaysSection`'s; this module is the dialog around them. The merchant is
 * the one the server recognised (nothing is built from the description in
 * the browser). Changing the existing transactions is a separate choice and
 * not the default. A row that cannot teach, or a merchant an earlier rule
 * already decides, is explained and cannot be saved.
 */
import "./transactions.css";

import { useState } from "react";

import { Button } from "../../components/Button";
import { CategoryPicker } from "../../components/CategoryPicker";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import type { Category, Transaction } from "./api";
import { AlwaysPreview, useAlwaysRule, type SavedRule } from "./AlwaysSection";

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
  const [categoryId, setCategoryId] = useState(startWith ?? transaction.category_id ?? transaction.suggested_category_id ?? "");
  const rule = useAlwaysRule({ transaction, categories, categoryId, enabled: true });
  const { data, savable, merchant } = rule;
  const raw = (transaction.raw_payee ?? "").trim();

  const save = async (): Promise<void> => {
    const saved = await rule.save();
    if (saved !== null) {
      onSaved(saved);
      onClose();
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
            <Button variant="primary" loading={rule.busy} onClick={() => void save()}>
              {data?.existing_rule_id == null ? "Save rule" : "Change the rule"}
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
            }}
          />
        </Field>
        <div aria-live="polite" className="tx-preview">
          <AlwaysPreview rule={rule} />
        </div>
        {rule.error === null ? null : (
          <p role="alert" className="ledger-error">
            {rule.error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
