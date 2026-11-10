// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Always categorise like this": turn one correction in the grid into a rule,
 * so the next import arrives categorised. The person sees the words the rule
 * will look for (taken from the row's description, theirs to edit), how many
 * transactions already match, and may apply it to those that have no
 * category yet. Rules belong to owners and admins (`POST /rules`).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { applyRules, createRule, testRule } from "../rules/api";
import { invalidateLedger, type Category, type Transaction } from "./api";
import { categoryRule, descriptionKey } from "./remember";

const MIN_CHARS = 3;
const MAX_CHARS = 200;

export interface RememberCategoryDialogProps {
  transaction: Transaction;
  categories: readonly Category[];
  onClose: () => void;
}

export function RememberCategoryDialog({ transaction, categories, onClose }: RememberCategoryDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [text, setText] = useState(() => descriptionKey(transaction.raw_payee));
  const [categoryId, setCategoryId] = useState(transaction.category_id ?? "");
  const [applyNow, setApplyNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wanted = text.trim();
  const category = categories.find((candidate) => candidate.id === categoryId);
  const ready = wanted.length >= MIN_CHARS && category !== undefined;

  // How many existing transactions the words match (a dry run; nothing changes).
  const preview = useQuery({
    queryKey: [...queryRoots.rules, "remember-preview", wanted, categoryId],
    queryFn: () => {
      // The dry run takes what a rule does, without its name or switch.
      const { conditions, actions, stop_processing } = categoryRule(wanted, categoryId, category?.name ?? "");
      return testRule({ rule: { conditions, actions, stop_processing }, limit: 1, only_changes: false });
    },
    enabled: ready,
    staleTime: 30_000,
  });

  const save = async (): Promise<void> => {
    if (category === undefined || wanted.length < MIN_CHARS) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const rule = await createRule(categoryRule(wanted, category.id, category.name));
      if (applyNow) {
        await applyRules({ rule_ids: [rule.id] });
      }
      await queryClient.invalidateQueries({ queryKey: queryRoots.rules });
      invalidateLedger(queryClient);
      toast.notify({
        tone: "success",
        title: "Rule saved",
        description: applyNow
          ? `Matching transactions without a category are being set to ${category.name}.`
          : `Matching transactions will be set to ${category.name} as they arrive.`,
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
      title="Always categorise like this"
      description="Saves a rule. It runs on every import from now on; you can change or remove it under Rules."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={!ready} loading={busy} onClick={() => void save()}>
            Save rule
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field
          label="When the description contains"
          hint={`From "${transaction.raw_payee ?? ""}". Keep the words that come back every time; leave out dates and reference numbers.`}
          error={wanted.length > 0 && wanted.length < MIN_CHARS ? "Use at least three characters." : undefined}
          required
        >
          <Input
            value={text}
            maxLength={MAX_CHARS}
            onChange={(event) => {
              setText(event.target.value);
            }}
          />
        </Field>
        <Field label="Set the category to" required>
          <Select
            placeholder="Choose a category"
            options={categories.map((candidate) => ({ value: candidate.id, label: candidate.name }))}
            value={categoryId}
            onChange={(event) => {
              setCategoryId(event.target.value);
            }}
          />
        </Field>
        <p className="ledger-muted" aria-live="polite">
          {!ready
            ? "Choose the words and a category to see what matches."
            : preview.isPending
              ? "Checking your transactions…"
              : preview.isError
                ? errorMessage(preview.error)
                : preview.data.matched === 0
                  ? "No transaction without a category matches these words yet."
                  : preview.data.matched === 1
                    ? "1 transaction without a category matches these words."
                    : `${String(preview.data.matched)} transactions without a category match these words.`}
        </p>
        <Checkbox
          label="Set it on those transactions now"
          checked={applyNow}
          onChange={(event) => {
            setApplyNow(event.target.checked);
          }}
        />
        <p className="ledger-muted">
          The rule only fills in a missing category; it never changes one that is already set. For a merchant with several spellings, a payee with aliases and a
          default category does the same job: <Link to="/settings/categories">Categories, payees and tags</Link>.
        </p>
        {error === null ? null : (
          <p role="alert" className="ledger-error">
            {error}
          </p>
        )}
      </div>
    </Dialog>
  );
}
