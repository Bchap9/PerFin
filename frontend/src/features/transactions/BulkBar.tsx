// SPDX-License-Identifier: AGPL-3.0-or-later
/** Bulk actions on the selected transactions. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { bulkAction, invalidateLedger, useCategories, useTags, type BulkAction } from "./api";

export function BulkBar({ ids, onDone }: { ids: readonly string[]; onDone: () => void }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const categories = useCategories();
  const tags = useTags();
  const [categoryId, setCategoryId] = useState("");
  const [tagId, setTagId] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const run = useMutation({
    mutationFn: (body: BulkAction) => bulkAction(body),
    onSuccess: (result) => {
      invalidateLedger(queryClient);
      const skipped = result.results.filter((item) => item.result === "skipped" || item.result === "not_found").length;
      toast.notify({
        title: `${String(result.changed)} transaction${result.changed === 1 ? "" : "s"} changed`,
        ...(skipped > 0 ? { description: `${String(skipped)} skipped.` } : {}),
        tone: skipped > 0 ? "warning" : "success",
      });
      setConfirmDelete(false);
      onDone();
    },
    onError: (error) => {
      toast.notify({ title: "Bulk action failed", description: errorMessage(error), tone: "error" });
    },
  });
  const send = (action: BulkAction["action"], extra: Partial<BulkAction> = {}): void => {
    run.mutate({ action, ids: [...ids], ...extra });
  };

  return (
    <div className="ledger-toolbar" role="group" aria-label={`Actions for ${String(ids.length)} selected`}>
      <strong>{ids.length} selected</strong>
      <Select
        aria-label="Bulk category"
        placeholder="Choose category"
        options={(categories.data ?? []).map((category) => ({ value: category.id, label: category.name }))}
        value={categoryId}
        onChange={(event) => {
          setCategoryId(event.target.value);
        }}
      />
      <Button
        disabled={categoryId === ""}
        loading={run.isPending}
        onClick={() => {
          send("categorise", { category_id: categoryId });
        }}
      >
        Set category
      </Button>
      <Select
        aria-label="Bulk tag"
        placeholder="Choose tag"
        options={(tags.data ?? []).map((tag) => ({ value: tag.id, label: tag.name }))}
        value={tagId}
        onChange={(event) => {
          setTagId(event.target.value);
        }}
      />
      <Button
        disabled={tagId === ""}
        onClick={() => {
          send("tag", { tag_ids: [tagId] });
        }}
      >
        Add tag
      </Button>
      <Button
        disabled={tagId === ""}
        onClick={() => {
          send("untag", { tag_ids: [tagId] });
        }}
      >
        Remove tag
      </Button>
      <Button
        onClick={() => {
          send("confirm");
        }}
      >
        Mark confirmed
      </Button>
      <Button
        variant="danger"
        onClick={() => {
          setConfirmDelete(true);
        }}
      >
        Delete
      </Button>
      <Dialog
        open={confirmDelete}
        onClose={() => {
          setConfirmDelete(false);
        }}
        title={`Delete ${String(ids.length)} transaction${ids.length === 1 ? "" : "s"}?`}
        description="Deleted transactions are removed from balances and reports."
        footer={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                setConfirmDelete(false);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={run.isPending}
              onClick={() => {
                send("delete");
              }}
            >
              {ids.length === 1 ? "Delete transaction" : "Delete transactions"}
            </Button>
          </>
        }
      />
    </div>
  );
}
