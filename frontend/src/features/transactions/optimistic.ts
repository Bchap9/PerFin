// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Optimistic edits of transactions held in the cached list pages.
 *
 * `useEditTransaction` applies a patch to every cached copy of the row at
 * once, sends the PATCH, and on failure puts the previous cache back and
 * reports the error (rollback). On settle the lists are refetched so the
 * server's answer (rules, derived fields) wins.
 */
import { useMutation, useQueryClient, type InfiniteData, type QueryClient, type QueryKey } from "@tanstack/react-query";

import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { txKeys, updateTransaction, type Transaction, type TransactionPage, type TransactionUpdate } from "./api";

/** The row with the fields of `patch` applied the way the server would apply them. */
export function applyPatch(row: Transaction, patch: TransactionUpdate): Transaction {
  const next: Transaction = { ...row };
  if (patch.category_id !== undefined) {
    next.category_id = patch.category_id;
  }
  if (patch.payee_id !== undefined) {
    next.payee_id = patch.payee_id;
  }
  if (patch.notes !== undefined) {
    next.notes = patch.notes;
  }
  if (patch.memo !== undefined) {
    next.memo = patch.memo;
  }
  if (patch.review_state != null) {
    next.review_state = patch.review_state;
  }
  if (patch.status != null) {
    next.status = patch.status;
  }
  if (patch.tag_ids != null) {
    next.tag_ids = [...patch.tag_ids];
  }
  return next;
}

type Cached = InfiniteData<TransactionPage, string | null> | undefined;

/** Pages with row `id` patched; pages without it are returned as they were. */
export function patchPages(data: Cached, id: string, patch: TransactionUpdate): Cached {
  if (data === undefined) {
    return data;
  }
  return {
    ...data,
    pages: data.pages.map((page) =>
      page.items.some((row) => row.id === id)
        ? { ...page, items: page.items.map((row) => (row.id === id ? applyPatch(row, patch) : row)) }
        : page,
    ),
  };
}

export interface EditContext {
  snapshots: [QueryKey, Cached][];
}

/** Apply `patch` to every cached list and return what is needed to undo it. */
export function applyOptimistic(queryClient: QueryClient, id: string, patch: TransactionUpdate): EditContext {
  const snapshots = queryClient.getQueriesData<InfiniteData<TransactionPage, string | null>>({ queryKey: txKeys.lists });
  for (const [key, data] of snapshots) {
    queryClient.setQueryData<Cached>(key, patchPages(data, id, patch));
  }
  return { snapshots };
}

export function rollback(queryClient: QueryClient, context: EditContext | undefined): void {
  for (const [key, data] of context?.snapshots ?? []) {
    queryClient.setQueryData<Cached>(key, data);
  }
}

export interface EditVars {
  id: string;
  patch: TransactionUpdate;
}

export function useEditTransaction() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation<Transaction, unknown, EditVars, EditContext>({
    mutationFn: ({ id, patch }) => updateTransaction(id, patch),
    onMutate: async ({ id, patch }) => {
      await queryClient.cancelQueries({ queryKey: txKeys.lists });
      return applyOptimistic(queryClient, id, patch);
    },
    onError: (error, _vars, context) => {
      rollback(queryClient, context);
      toast.notify({ title: "Could not save the change", description: errorMessage(error), tone: "error" });
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
    },
  });
}
