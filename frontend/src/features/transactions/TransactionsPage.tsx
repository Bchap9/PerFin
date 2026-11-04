// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useInfiniteQuery } from "@tanstack/react-query";
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
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import {
  EMPTY_FILTERS,
  listTransactions,
  txKeys,
  useCategories,
  usePayees,
  useTags,
  type ReviewState,
  type Transaction,
  type TransactionFilters,
} from "./api";
import { BulkBar } from "./BulkBar";
import { CategoryCell, NotesCell, PayeeCell, rowLabel, TagsCell } from "./cells";
import { SplitEditor } from "./SplitEditor";
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
  const account = search["account"];
  return { ...EMPTY_FILTERS, account: typeof account === "string" ? account : "" };
}

export function TransactionsPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
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

  const query = useInfiniteQuery({
    queryKey: txKeys.list(filters),
    queryFn: ({ pageParam }) => listTransactions(filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
  const rows = useMemo(() => query.data?.pages.flatMap((page) => page.items) ?? [], [query.data]);
  const accountsById = useMemo(() => new Map((accounts.data ?? []).map((account) => [account.id, account])), [accounts.data]);
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
    { id: "date", header: "Date", rowHeader: true, cell: (row) => formatDate(row.posted_date) ?? row.posted_date },
    {
      id: "account",
      header: "Account",
      cell: (row) => {
        const account = accountsById.get(row.account_id);
        return account === undefined ? "" : accountLabel(account);
      },
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
      cell: (row) => <TagsCell row={row} editable={editable(row)} label={rowLabel(row, payeeList)} tags={tagList} />,
    },
    {
      id: "notes",
      header: "Notes",
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
      cell: (row) => <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="signed" />,
    },
    ...(canWrite
      ? [
          {
            id: "actions",
            header: <span className="visually-hidden">Actions</span>,
            cell: (row: Transaction) =>
              editable(row) ? (
                <span className="ledger-actions">
                  <Button
                    size="sm"
                    aria-label={`Split ${rowLabel(row, payeeList)}`}
                    onClick={() => {
                      setSplitting(row);
                    }}
                  >
                    Split
                  </Button>
                  <Button
                    size="sm"
                    aria-label={`${row.transfer === null ? "Link transfer for" : "Unlink transfer of"} ${rowLabel(row, payeeList)}`}
                    onClick={() => {
                      setTransferring(row);
                    }}
                  >
                    {row.transfer === null ? "Transfer" : "Unlink"}
                  </Button>
                </span>
              ) : null,
          },
        ]
      : []),
  ];

  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Transactions</h1>
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
