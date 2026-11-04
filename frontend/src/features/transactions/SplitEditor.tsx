// SPDX-License-Identifier: AGPL-3.0-or-later
/** Split editor: lines must add up exactly to the transaction amount (decimal strings, no floats). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { setSplits, useCategories, type Transaction } from "./api";
import { normalizeAmountInput, suggestedRemainder, validateSplits, type SplitDraft } from "./splits";

let keySeq = 0;
function nextKey(): string {
  keySeq += 1;
  return `line-${String(keySeq)}`;
}

function draftsOf(transaction: Transaction): SplitDraft[] {
  if (transaction.splits.length >= 2) {
    return transaction.splits.map((split) => ({
      key: nextKey(),
      amount: split.amount.amount,
      categoryId: split.category_id ?? "",
      memo: split.memo ?? "",
    }));
  }
  return [
    { key: nextKey(), amount: transaction.amount.amount, categoryId: transaction.category_id ?? "", memo: "" },
    { key: nextKey(), amount: "", categoryId: "", memo: "" },
  ];
}

export function SplitEditor({ transaction, onClose }: { transaction: Transaction; onClose: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const categories = useCategories();
  const [lines, setLines] = useState<SplitDraft[]>(() => draftsOf(transaction));
  const [serverError, setServerError] = useState<string | null>(null);
  const total = transaction.amount;
  const check = validateSplits(total.amount, lines);
  const hadSplits = transaction.splits.length > 0;

  const save = useMutation({
    mutationFn: (payload: { remove: boolean }) =>
      setSplits(
        transaction.id,
        payload.remove
          ? []
          : lines.map((line) => ({
              amount: { amount: normalizeAmountInput(line.amount) ?? line.amount, currency: total.currency },
              category_id: line.categoryId === "" ? null : line.categoryId,
              memo: line.memo.trim() === "" ? null : line.memo.trim(),
              tax_flag: false,
            })),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
      toast.notify({ title: "Split saved", tone: "success" });
      onClose();
    },
    onError: (error) => {
      setServerError(errorMessage(error));
    },
  });

  const update = (key: string, change: Partial<SplitDraft>): void => {
    setLines((current) => current.map((line) => (line.key === key ? { ...line, ...change } : line)));
  };
  const categoryOptions = (categories.data ?? []).map((category) => ({ value: category.id, label: category.name }));

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Split transaction"
      description={
        <>
          Total <MoneyText amount={total.amount} currency={total.currency} tone="none" />. The lines must add up exactly.
        </>
      }
      footer={
        <>
          {hadSplits ? (
            <Button
              variant="danger"
              loading={save.isPending}
              onClick={() => {
                save.mutate({ remove: true });
              }}
            >
              Remove split
            </Button>
          ) : null}
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={!check.ok}
            loading={save.isPending}
            onClick={() => {
              save.mutate({ remove: false });
            }}
          >
            Save split
          </Button>
        </>
      }
    >
      <div className="ledger-stack">
        {lines.map((line, index) => (
          <div className="split-line" key={line.key} role="group" aria-label={`Split line ${String(index + 1)}`}>
            <Field label="Amount" error={check.lineErrors.get(index)}>
              <Input
                value={line.amount}
                inputMode="decimal"
                onChange={(event) => {
                  update(line.key, { amount: event.target.value });
                }}
              />
            </Field>
            <Field label="Category">
              <Select
                placeholder="Uncategorised"
                options={categoryOptions}
                value={line.categoryId}
                onChange={(event) => {
                  update(line.key, { categoryId: event.target.value });
                }}
              />
            </Field>
            <Field label="Memo">
              <Input
                value={line.memo}
                maxLength={500}
                onChange={(event) => {
                  update(line.key, { memo: event.target.value });
                }}
              />
            </Field>
            <Button
              variant="ghost"
              disabled={lines.length <= 2}
              aria-label={`Remove line ${String(index + 1)}`}
              onClick={() => {
                setLines((current) => current.filter((item) => item.key !== line.key));
              }}
            >
              Remove
            </Button>
          </div>
        ))}
        <div className="ledger-actions">
          <Button
            onClick={() => {
              const rest = suggestedRemainder(total.amount, lines);
              setLines((current) => [...current, { key: nextKey(), amount: rest === "0" ? "" : rest, categoryId: "", memo: "" }]);
            }}
          >
            Add line
          </Button>
          <p role="status" className={check.formError === null ? "ledger-muted" : "ledger-error"}>
            {check.ok
              ? "The lines add up."
              : (check.formError ?? "Fix the amounts to continue.")}
          </p>
        </div>
        {serverError === null ? null : (
          <p role="alert" className="ledger-error">
            {serverError}
          </p>
        )}
      </div>
    </Dialog>
  );
}
