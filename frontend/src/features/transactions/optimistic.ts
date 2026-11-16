// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Optimistic edits of transactions held in the cached list pages.
 *
 * `useEditTransaction` applies a patch to every cached copy of the row at
 * once (the list pages and the row the drawer reads by id), sends the PATCH, and on failure puts the previous cache back and
 * reports the error (rollback). On settle the lists are refetched so the
 * server's answer (rules, derived fields) wins.
 */
import { useMutation, useQueryClient, type InfiniteData, type QueryClient, type QueryKey } from "@tanstack/react-query";

import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import {
  bulkAction,
  invalidateLedger,
  txKeys,
  updateTransaction,
  type BulkResult,
  type Transaction,
  type TransactionPage,
  type TransactionUpdate,
} from "./api";
import { isSuggestion } from "./provenance";

/** The row with the fields of `patch` applied the way the server would apply them. */
export function applyPatch(row: Transaction, patch: TransactionUpdate): Transaction {
  const next: Transaction = { ...row };
  if (patch.category_id !== undefined) {
    // A category set or cleared by hand is the person's choice, and it ends any suggestion.
    Object.assign(next, decidedByPerson(patch.category_id));
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

/** The category fields of a row after a person decided its category. */
function decidedByPerson(categoryId: string | null): Partial<Transaction> {
  return {
    category_id: categoryId,
    category_source: "person",
    category_confidence: "1.00",
    category_rule_id: null,
    suggested_category_id: null,
  };
}

/** The row after its suggestion was accepted; a row without one is returned as it was. */
export function acceptSuggestion(row: Transaction): Transaction {
  return isSuggestion(row) ? { ...row, ...decidedByPerson(row.suggested_category_id ?? null) } : row;
}

type Cached = InfiniteData<TransactionPage, string | null> | undefined;

function mapRow(data: Cached, id: string, change: (row: Transaction) => Transaction): Cached {
  if (data === undefined) {
    return data;
  }
  return {
    ...data,
    pages: data.pages.map((page) =>
      page.items.some((row) => row.id === id) ? { ...page, items: page.items.map((row) => (row.id === id ? change(row) : row)) } : page,
    ),
  };
}

/** Pages with row `id` patched; pages without it are returned as they were. */
export function patchPages(data: Cached, id: string, patch: TransactionUpdate): Cached {
  return mapRow(data, id, (row) => applyPatch(row, patch));
}

export interface EditContext {
  snapshots: [QueryKey, Cached][];
  /** The row read by id (the drawer's), as it was. */
  one?: [QueryKey, Transaction];
}

/** Apply `patch` to every cached list and return what is needed to undo it. */
export function applyOptimistic(queryClient: QueryClient, id: string, patch: TransactionUpdate): EditContext {
  return changeEverywhere(queryClient, id, (row) => applyPatch(row, patch));
}

function changeEverywhere(queryClient: QueryClient, id: string, change: (row: Transaction) => Transaction): EditContext {
  const snapshots = queryClient.getQueriesData<InfiniteData<TransactionPage, string | null>>({ queryKey: txKeys.lists });
  for (const [key, data] of snapshots) {
    queryClient.setQueryData<Cached>(key, mapRow(data, id, change));
  }
  const oneKey = txKeys.one(id);
  const one = queryClient.getQueryData<Transaction>(oneKey);
  if (one === undefined) {
    return { snapshots };
  }
  queryClient.setQueryData<Transaction>(oneKey, change(one));
  return { snapshots, one: [oneKey, one] };
}

export function rollback(queryClient: QueryClient, context: EditContext | undefined): void {
  for (const [key, data] of context?.snapshots ?? []) {
    queryClient.setQueryData<Cached>(key, data);
  }
  if (context?.one !== undefined) {
    queryClient.setQueryData<Transaction>(context.one[0], context.one[1]);
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
      await queryClient.cancelQueries({ queryKey: txKeys.one(id) });
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

/**
 * Accept the suggested category of one row. It shows at once as the person's
 * choice, is put back if the server refuses, and the server is told through
 * the bulk action (`accept_suggestions` with the one id).
 */
export function useAcceptSuggestion() {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation<BulkResult, unknown, string, EditContext>({
    mutationFn: (id) => bulkAction({ action: "accept_suggestions", ids: [id] }),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: txKeys.lists });
      await queryClient.cancelQueries({ queryKey: txKeys.one(id) });
      return changeEverywhere(queryClient, id, acceptSuggestion);
    },
    onSuccess: (result) => {
      if (result.changed === 0) {
        // Somebody else, or a refresh, got there first; the refetch shows what is true now.
        toast.notify({ title: "That suggestion is no longer there", tone: "warning" });
      }
    },
    onError: (error, _id, context) => {
      rollback(queryClient, context);
      toast.notify({ title: "Could not accept the suggestion", description: errorMessage(error), tone: "error" });
    },
    onSettled: () => {
      // A category counts in budgets and reports; a suggestion did not.
      invalidateLedger(queryClient);
    },
  });
}
