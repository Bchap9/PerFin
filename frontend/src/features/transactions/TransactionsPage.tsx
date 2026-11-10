// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { useMemo, useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate, isIsoDate } from "../../lib/dates";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import { canManageMembers, useAuthSession } from "../auth/session";
import {
  autoPairTransfers,
  EMPTY_FILTERS,
  invalidateLedger,
  listTransactions,
  txKeys,
  useCategories,
  usePayees,
  useTags,
  type ReviewState,
  type Transaction,
  type TransactionFilters,
} from "./api";
import { autoPairSummary } from "./autoPair";
import { BulkBar } from "./BulkBar";
import { CategoryCell, NotesCell, PayeeCell, rowLabel, TagsCell } from "./cells";
import { DeleteTransactionDialog } from "./DeleteTransactionDialog";
import { useEditTransaction } from "./optimistic";
import { RememberCategoryDialog } from "./RememberCategoryDialog";
import { RowActions, type RowAction } from "./RowActions";
import { SplitEditor } from "./SplitEditor";
import { TransactionDialog } from "./TransactionDialog";
import { TransferDialog } from "./TransferDialog";

const REVIEW_STATES: readonly { value: ReviewState; label: string }[] = [
  { value: "none", label: "Not flagged" },
  { value: "pending", label: "Needs review" },
  { value: "confirmed", label: "Confirmed" },
];

function isReviewState(value: string): value is ReviewState | "" {
  return value === "" || REVIEW_STATES.some((state) => state.value === value);
}

function initialFilters(search: Record<string, unknown>): TransactionFilters {
  // A link from a report or an account page narrows the list: `account`,
  // `category` (an id, or "none"), `date_from` and `date_to`, named like the
  // filters of GET /transactions.
  const given = new Map(Object.entries(search));
  const text = (key: string): string => {
    const value = given.get(key);
    return typeof value === "string" ? value : "";
  };
  const day = (key: string): string => (isIsoDate(text(key)) ? text(key) : "");
  return { ...EMPTY_FILTERS, account: text("account"), category: text("category"), dateFrom: day("date_from"), dateTo: day("date_to") };
}

export function TransactionsPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const queryClient = useQueryClient();
  const toast = useToast();
  const canWrite = useCanWrite();
  const accounts = useAccounts();
  const categories = useCategories();
  const payees = usePayees();
  const tags = useTags();
  const [draft, setDraft] = useState<TransactionFilters>(() => initialFilters(search));
  const [filters, setFilters] = useState<TransactionFilters>(draft);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [splitting, setSplitting] = useState<Transaction | null>(null);
  const [transferring, setTransferring] = useState<Transaction | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Transaction | null>(null);
  const [deleting, setDeleting] = useState<Transaction | null>(null);
  const [remembering, setRemembering] = useState<Transaction | null>(null);
  // Rules belong to owners and admins (POST /rules).
  const { session } = useAuthSession();
  const canMakeRules = canManageMembers(session?.household?.role);
  const edit = useEditTransaction();
  const findTransfers = useMutation({
    mutationFn: autoPairTransfers,
    onSuccess: (result) => {
      invalidateLedger(queryClient);
      // A result with something left to do by hand stays up longer.
      toast.notify({ ...autoPairSummary(result), ...(result.ambiguous > 0 ? { durationMs: 15_000 } : {}) });
    },
    onError: (error) => {
      toast.notify({ title: "Could not look for transfers", description: errorMessage(error), tone: "error" });
    },
  });

  const query = useInfiniteQuery({
    queryKey: txKeys.list(filters),
    queryFn: ({ pageParam }) => listTransactions(filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = useMemo(() => query.data?.pages.flatMap((page) => page.items) ?? [], [query.data]);
  const accountsById = useMemo(() => new Map((accounts.data ?? []).map((account) => [account.id, account])), [accounts.data]);
  const accountLabelOf = (id: string): string | undefined => {
    const account = accountsById.get(id);
    return account === undefined ? undefined : accountLabel(account);
  };
  const categoryList = categories.data ?? [];
  const payeeList = payees.data ?? [];
  const tagList = tags.data ?? [];

  const setField = <K extends keyof TransactionFilters>(key: K, value: TransactionFilters[K]): void => {
    setDraft((current) => ({ ...current, [key]: value }));
  };
  const apply = (event: FormEvent): void => {
    event.preventDefault();
    setSelected(new Set());
    setFilters(draft);
  };
  const reset = (): void => {
    setDraft(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
    setSelected(new Set());
  };

  const editable = (row: Transaction): boolean => canWrite && accountsById.get(row.account_id)?.can_edit === true;
  const selectableRows = rows.filter(editable);
  const allSelected = selectableRows.length > 0 && selectableRows.every((row) => selected.has(row.id));
  const canAdd = canWrite && (accounts.data ?? []).some((account) => account.can_edit);

  const confirmRow = (row: Transaction): void => {
    edit.mutate(
      { id: row.id, patch: { review_state: "confirmed" } },
      {
        onSuccess: () => {
          // Confirmed rows count in reports and budgets.
          invalidateLedger(queryClient);
          toast.notify({ title: "Marked as confirmed", tone: "success" });
        },
      },
    );
  };
  const actionsFor = (row: Transaction): RowAction[] => [
    {
      id: "edit",
      label: "Edit",
      onSelect: () => {
        setEditing(row);
      },
    },
    {
      id: "split",
      label: row.splits.length > 0 ? "Edit split" : "Split",
      onSelect: () => {
        setSplitting(row);
      },
    },
    {
      id: "transfer",
      label: row.transfer === null ? "Link transfer" : "Unlink transfer",
      onSelect: () => {
        setTransferring(row);
      },
    },
    ...(canMakeRules && row.splits.length === 0 && (row.raw_payee ?? "").trim() !== ""
      ? [
          {
            id: "remember",
            label: "Always categorise like this…",
            onSelect: () => {
              setRemembering(row);
            },
          },
        ]
      : []),
    ...(row.review_state === "pending"
      ? [
          {
            id: "confirm",
            label: "Mark confirmed",
            onSelect: () => {
              confirmRow(row);
            },
          },
        ]
      : []),
    {
      id: "delete",
      label: "Delete",
      danger: true,
      onSelect: () => {
        setDeleting(row);
      },
    },
  ];

  const columns: TableColumn<Transaction>[] = [
    ...(canWrite
      ? [
          {
            id: "select",
            header: (
              <Checkbox
                label={<span className="visually-hidden">Select all loaded transactions</span>}
                checked={allSelected}
                onChange={(event) => {
                  setSelected(event.target.checked ? new Set(selectableRows.map((row) => row.id)) : new Set());
                }}
              />
            ),
            cell: (row: Transaction) =>
              editable(row) ? (
                <Checkbox
                  label={<span className="visually-hidden">{`Select ${rowLabel(row, payeeList)}`}</span>}
                  checked={selected.has(row.id)}
                  onChange={(event) => {
                    setSelected((current) => {
                      const next = new Set(current);
                      if (event.target.checked) {
                        next.add(row.id);
                      } else {
                        next.delete(row.id);
                      }
                      return next;
                    });
                  }}
                />
              ) : null,
          },
        ]
      : []),
    { id: "date", header: "Date", rowHeader: true, className: "tx-nowrap", cell: (row) => formatDate(row.posted_date) ?? row.posted_date },
    {
      id: "account",
      header: "Account",
      className: "tx-col-account",
      cell: (row) => accountLabelOf(row.account_id) ?? "",
    },
    {
      id: "payee",
      header: "Payee",
      cell: (row) => <PayeeCell row={row} editable={editable(row)} label={rowLabel(row, payeeList)} payees={payeeList} />,
    },
    {
      id: "category",
      header: "Category",
      cell: (row) => <CategoryCell row={row} editable={editable(row)} label={rowLabel(row, payeeList)} categories={categoryList} />,
    },
    {
      id: "tags",
      header: "Tags",
      className: "tx-col-tags",
      cell: (row) => <TagsCell row={row} editable={editable(row)} label={rowLabel(row, payeeList)} tags={tagList} />,
    },
    {
      id: "notes",
      header: "Notes",
      className: "tx-col-notes",
      cell: (row) => <NotesCell row={row} editable={editable(row)} label={rowLabel(row, payeeList)} />,
    },
    {
      id: "status",
      header: "State",
      cell: (row) => (
        <span className="ledger-actions">
          {row.status === "pending" ? <Badge tone="warning">Pending</Badge> : null}
          {row.review_state === "pending" ? <Badge tone="info">Needs review</Badge> : null}
          {row.transfer === null ? null : <Badge>Transfer</Badge>}
        </span>
      ),
    },
    {
      id: "amount",
      header: "Amount",
      align: "end",
      className: "tx-nowrap",
      cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="signed" />,
    },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: <span className="visually-hidden">Actions</span>,
            align: "end" as const,
            className: "tx-col-actions",
            cell: (row: Transaction) => (editable(row) ? <RowActions label={rowLabel(row, payeeList)} actions={actionsFor(row)} /> : null),
          },
        ]
      : []),
  ];

  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Transactions</h1>
        {canAdd ? (
          <div className="ledger-actions">
            <Button
              loading={findTransfers.isPending}
              title="Link matching money out and money in between your accounts as transfers"
              onClick={() => {
                findTransfers.mutate();
              }}
            >
              Find transfers
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setAdding(true);
              }}
            >
              Add transaction
            </Button>
          </div>
        ) : null}
      </div>
      {canWrite ? null : <p className="ledger-muted">You have read-only access.</p>}
      <Card title="Filters">
        <form className="ledger-filters" aria-label="Filter transactions" onSubmit={apply}>
          <Field label="Account">
            <Select
              placeholder="All accounts"
              options={(accounts.data ?? []).map((account) => ({ value: account.id, label: accountLabel(account) }))}
              value={draft.account}
              onChange={(event) => {
                setField("account", event.target.value);
              }}
            />
          </Field>
          <Field label="From">
            <Input
              type="date"
              value={draft.dateFrom}
              onChange={(event) => {
                setField("dateFrom", event.target.value);
              }}
            />
          </Field>
          <Field label="To">
            <Input
              type="date"
              value={draft.dateTo}
              onChange={(event) => {
                setField("dateTo", event.target.value);
              }}
            />
          </Field>
          <Field label="Category filter">
            <Select
              placeholder="Any category"
              options={[{ value: "none", label: "Uncategorised" }, ...categoryList.map((category) => ({ value: category.id, label: category.name }))]}
              value={draft.category}
              onChange={(event) => {
                setField("category", event.target.value);
              }}
            />
          </Field>
          <Field label="Payee filter">
            <Select
              placeholder="Any payee"
              options={payeeList.map((payee) => ({ value: payee.id, label: payee.name }))}
              value={draft.payee}
              onChange={(event) => {
                setField("payee", event.target.value);
              }}
            />
          </Field>
          <Field label="Tag filter">
            <Select
              placeholder="Any tag"
              options={tagList.map((tag) => ({ value: tag.id, label: tag.name }))}
              value={draft.tag}
              onChange={(event) => {
                setField("tag", event.target.value);
              }}
            />
          </Field>
          <Field label="Review state">
            <Select
              placeholder="Any state"
              options={REVIEW_STATES}
              value={draft.reviewState}
              onChange={(event) => {
                if (isReviewState(event.target.value)) {
                  setField("reviewState", event.target.value);
                }
              }}
            />
          </Field>
          <Field label="Search">
            <Input
              type="search"
              value={draft.q}
              maxLength={100}
              onChange={(event) => {
                setField("q", event.target.value);
              }}
            />
          </Field>
          <div className="ledger-actions">
            <Button type="submit" variant="primary">
              Apply filters
            </Button>
            <Button onClick={reset}>Clear</Button>
          </div>
        </form>
      </Card>
      {selected.size > 0 ? (
        <BulkBar
          ids={[...selected]}
          onDone={() => {
            setSelected(new Set());
          }}
        />
      ) : null}
      {query.isPending ? <Spinner label="Loading transactions" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : null}
      {query.isSuccess ? (
        <Table
          caption="Transactions"
          captionHidden
          maxHeight="none"
          density="compact"
          className="tx-grid"
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          empty="No transactions match."
        />
      ) : null}
      {query.hasNextPage ? (
        <div className="ledger-actions">
          <Button
            loading={query.isFetchingNextPage}
            onClick={() => {
              void query.fetchNextPage();
            }}
          >
            Load more
          </Button>
        </div>
      ) : null}
      {splitting === null ? null : (
        <SplitEditor
          transaction={splitting}
          onClose={() => {
            setSplitting(null);
          }}
        />
      )}
      {adding ? (
        <TransactionDialog
          {...(accountsById.get(filters.account)?.can_edit === true ? { defaultAccountId: filters.account } : {})}
          onClose={() => {
            setAdding(false);
          }}
        />
      ) : null}
      {editing === null ? null : (
        <TransactionDialog
          transaction={editing}
          onClose={() => {
            setEditing(null);
          }}
        />
      )}
      {deleting === null ? null : (
        <DeleteTransactionDialog
          transaction={deleting}
          label={rowLabel(deleting, payeeList)}
          accountName={accountLabelOf(deleting.account_id)}
          onDeleted={(id) => {
            setSelected((current) => {
              const next = new Set(current);
              next.delete(id);
              return next;
            });
          }}
          onClose={() => {
            setDeleting(null);
          }}
        />
      )}
      {remembering === null ? null : (
        <RememberCategoryDialog
          transaction={remembering}
          categories={categoryList}
          onClose={() => {
            setRemembering(null);
          }}
        />
      )}
      {transferring === null ? null : (
        <TransferDialog
          transaction={transferring}
          onClose={() => {
            setTransferring(null);
          }}
        />
      )}
    </section>
  );
}
