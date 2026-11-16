// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";
import "./transactions.css";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
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
import { AlwaysCategoriseDialog, RuleSavedNotice, type SavedRule } from "./AlwaysCategoriseDialog";
import {
  autoPairTransfers,
  canTeach,
  EMPTY_FILTERS,
  getTransaction,
  invalidateLedger,
  listTransactions,
  merchantName,
  txKeys,
  useCategories,
  useCategorisationSummary,
  useRuleNames,
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
import { isSuggestion } from "./provenance";
import { RowActions, type RowAction } from "./RowActions";
import { SplitEditor } from "./SplitEditor";
import { dismissTeach, teachDismissed, TeachPrompt, type TeachOffer } from "./TeachPrompt";
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
  // A link from a report, an account page or a document narrows the list:
  // `account`, `category` (an id, or "none"), `date_from`, `date_to` and
  // `document` (the file the rows were imported from) and `subscription` (the
  // series the charges belong to), named like the filters of GET
  // /transactions. `suggested=true` shows only the rows with a suggestion.
  // (`always` is not a filter: see `linkedTransactionId`.)
  const given = new Map(Object.entries(search));
  const text = (key: string): string => {
    const value = given.get(key);
    return typeof value === "string" ? value : "";
  };
  const day = (key: string): string => (isIsoDate(text(key)) ? text(key) : "");
  return {
    ...EMPTY_FILTERS,
    account: text("account"),
    category: text("category"),
    dateFrom: day("date_from"),
    dateTo: day("date_to"),
    document: text("document"),
    subscription: text("subscription"),
    suggested: given.get("suggested") === true || text("suggested") === "true",
  };
}

/**
 * `?always=<transaction id>`: "Make it a rule" on the learned page links here
 * to open "Always categorise <merchant> as..." for that transaction.
 */
function linkedTransactionId(search: Record<string, unknown>): string | null {
  const value = new Map(Object.entries(search)).get("always");
  return typeof value === "string" && value !== "" ? value : null;
}

export function TransactionsPage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const navigate = useNavigate();
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
  // "Always categorise <merchant> as...": the dialog, the offer after a
  // correction in the grid (one at a time) and the rule that was just saved.
  const [always, setAlways] = useState<TeachOffer | { transaction: Transaction; categoryId?: undefined } | null>(null);
  const [offer, setOffer] = useState<TeachOffer | null>(null);
  const [savedRule, setSavedRule] = useState<SavedRule | null>(null);
  // A link that names a transaction opens the dialog for it until it is
  // closed: the row is asked for by id because it need not be on the first
  // page of the grid. Closing also takes `always` out of the address, so a
  // reload or the back button does not open it again.
  const linkedId = canWrite ? linkedTransactionId(search) : null;
  const [linkClosed, setLinkClosed] = useState(false);
  const linked = useQuery({
    queryKey: txKeys.one(linkedId ?? ""),
    queryFn: () => getTransaction(linkedId ?? ""),
    enabled: linkedId !== null && !linkClosed,
  });
  const linkedRow = linkClosed ? undefined : linked.data;
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
  const summary = useCategorisationSummary();
  const ruleNames = useRuleNames(rows.some((row) => row.category_source === "rule" && row.category_rule_id != null));
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
  // The file filter has no field in the form: it comes from a link and goes
  // with its chip, leaving the other filters as they are.
  const showEveryFile = (): void => {
    setDraft((current) => ({ ...current, document: "" }));
    setFilters((current) => ({ ...current, document: "" }));
    setSelected(new Set());
  };
  // Likewise the charges of one subscription ("See in transactions").
  const showEverySeries = (): void => {
    setDraft((current) => ({ ...current, subscription: "" }));
    setFilters((current) => ({ ...current, subscription: "" }));
    setSelected(new Set());
  };

  // The two quick filters apply at once and leave the rest of the form as it is.
  const toggleSuggested = (): void => {
    const next = !filters.suggested;
    setDraft((current) => ({ ...current, suggested: next }));
    setFilters((current) => ({ ...current, suggested: next }));
    setSelected(new Set());
  };
  const toggleUncategorised = (): void => {
    const next = filters.category === "none" ? "" : "none";
    setDraft((current) => ({ ...current, category: next }));
    setFilters((current) => ({ ...current, category: next }));
    setSelected(new Set());
  };
  const counted = (label: string, count: number | undefined): string => (count === undefined ? label : `${label} (${String(count)})`);

  const editable = (row: Transaction): boolean => canWrite && accountsById.get(row.account_id)?.can_edit === true;
  // The link opens the dialog only where the row's own menu would offer it.
  const alwaysOpen =
    always ?? (linkedRow !== undefined && canTeach(linkedRow) && editable(linkedRow) ? { transaction: linkedRow, categoryId: undefined } : null);
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
    // A merchant rule is for members and above, which is everyone who has this menu.
    ...(canTeach(row)
      ? [
          {
            id: "always",
            label: `Always categorise ${merchantName(row) ?? "this merchant"} as…`,
            onSelect: () => {
              setAlways({ transaction: row });
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

  // A correction on a row that names a merchant may be worth a rule; the
  // prompt asks the server and stays silent when it is not.
  const offerRule = (row: Transaction, categoryId: string): void => {
    if (canTeach(row) && !teachDismissed(row.merchant_key ?? "")) {
      setOffer({ transaction: row, categoryId });
    }
  };

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
      cell: (row) => (
        <CategoryCell
          row={row}
          editable={editable(row)}
          label={rowLabel(row, payeeList)}
          categories={categoryList}
          ruleNames={ruleNames}
          onCategorised={offerRule}
        />
      ),
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
      {filters.document === "" ? null : (
        <p className="ledger-actions">
          <Link to="/documents/$documentId" params={{ documentId: filters.document }}>
            From one file
          </Link>
          <Button variant="ghost" size="sm" aria-label="Remove the file filter" onClick={showEveryFile}>
            Remove
          </Button>
        </p>
      )}
      {filters.subscription === "" ? null : (
        <p className="ledger-actions">
          <Link to="/subscriptions/$id" params={{ id: filters.subscription }}>
            Charges of one subscription
          </Link>
          <Button variant="ghost" size="sm" aria-label="Remove the subscription filter" onClick={showEverySeries}>
            Remove
          </Button>
        </p>
      )}
      <div className="tx-quick-filters" role="group" aria-label="Quick filters">
        <Button size="sm" aria-pressed={filters.suggested} onClick={toggleSuggested}>
          {counted("Suggestions", summary.data?.suggestions)}
        </Button>
        <Button size="sm" aria-pressed={filters.category === "none"} onClick={toggleUncategorised}>
          {counted("No category", summary.data?.uncategorised)}
        </Button>
      </div>
      {savedRule === null ? null : (
        <RuleSavedNotice
          key={`${savedRule.result.rule.id}:${savedRule.result.rule.updated_at}`}
          saved={savedRule}
          onDone={() => {
            setSavedRule(null);
          }}
        />
      )}
      {offer === null ? null : (
        <TeachPrompt
          offer={offer}
          categories={categoryList}
          onAlways={() => {
            setAlways(offer);
          }}
          onDismiss={() => {
            dismissTeach(offer.transaction.merchant_key ?? "");
            setOffer(null);
          }}
        />
      )}
      {selected.size > 0 ? (
        <BulkBar
          ids={[...selected]}
          suggestionIds={rows.filter((row) => selected.has(row.id) && isSuggestion(row)).map((row) => row.id)}
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
      {alwaysOpen === null ? null : (
        <AlwaysCategoriseDialog
          transaction={alwaysOpen.transaction}
          categories={categoryList}
          categoryId={alwaysOpen.categoryId}
          onClose={() => {
            setAlways(null);
            setLinkClosed(true);
            if (linkedId !== null) {
              const rest = Object.fromEntries(Object.entries(search).filter(([name]) => name !== "always"));
              void navigate({ to: "/transactions", search: rest, replace: true });
            }
          }}
          onSaved={(saved) => {
            setSavedRule(saved);
            // The merchant has its rule: the offer for it is answered.
            setOffer((current) => (current?.transaction.merchant_key === alwaysOpen.transaction.merchant_key ? null : current));
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
