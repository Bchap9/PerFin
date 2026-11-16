// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Transactions: one list of every account. Every filter lives in the address
 * (`filters.ts`) and is written back with `replace` as it changes, so the
 * rail link, which carries no search, clears them. When anything narrows the
 * list a total row says what the rows shown add up to.
 *
 * A row opens with its first cell's button (Table `rowLink`); Up and Down
 * move between those buttons, one of which is in the tab order at a time.
 * Opening a row puts `?tx=<id>` in the address (pushed, so Back closes the
 * drawer); `?always=<id>` opens the drawer for that row with "Always use…"
 * ticked. Closing takes both out and gives focus back to the row.
 */
import "../accounts/ledger.css";
import "./transactions.css";

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { Button } from "../../components/Button";
import { CategoryPicker } from "../../components/CategoryPicker";
import { Checkbox } from "../../components/Checkbox";
import { Chip } from "../../components/Chip";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { PageHeader } from "../../components/PageHeader";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate, isIsoDate } from "../../lib/dates";
import { formatMoney } from "../../lib/money";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import { AlwaysCategoriseDialog } from "./AlwaysCategoriseDialog";
import { RuleSavedNotice, type SavedRule } from "./AlwaysSection";
import {
  autoPairTransfers,
  canTeach,
  invalidateLedger,
  merchantName,
  txKeys,
  useCategories,
  useCategorisationSummary,
  useRuleNames,
  usePayees,
  useTags,
  type ReviewState,
  type Transaction,
} from "./api";
import { autoPairSummary } from "./autoPair";
import { BulkBar } from "./BulkBar";
import { CategoryCell, displayName, MerchantCell, NoteDialog, rowLabel } from "./cells";
import { DeleteTransactionDialog } from "./DeleteTransactionDialog";
import { fromSearch, hasFilters, listFiltered, NO_FILTERS, totals, withFilters, type ListFilters } from "./filters";
import { useEditTransaction } from "./optimistic";
import { isSuggestion } from "./provenance";
import { RowActions, type RowAction } from "./RowActions";
import { SplitEditor } from "./SplitEditor";
import { TransactionDialog } from "./TransactionDialog";
import { TransactionDrawer } from "./TransactionDrawer";
import { TransferDialog } from "./TransferDialog";

const REVIEW_STATES: readonly { value: ReviewState; label: string }[] = [
  { value: "none", label: "Not flagged" },
  { value: "pending", label: "Needs review" },
  { value: "confirmed", label: "Confirmed" },
];

function reviewState(value: string): ReviewState | "" {
  return REVIEW_STATES.find((state) => state.value === value)?.value ?? "";
}

/** A transaction id from the address: `tx` (the open drawer) or `always` (the drawer with "Always use…" ticked). */
function idParam(search: Record<string, unknown>, key: "tx" | "always"): string | null {
  const value = new Map(Object.entries(search)).get(key);
  return typeof value === "string" && value !== "" ? value : null;
}

/** The search without the drawer's keys: what is left when it closes. */
function withoutDrawer(search: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(search).filter(([name]) => name !== "tx" && name !== "always"));
}

const ROW_LINK = "button.table__row-link";

/** "Open Corner Market, Nov 1, 2026, -$42.10": the name of a row's link button. */
function openLabel(row: Transaction, name: string): string {
  const date = formatDate(row.posted_date) ?? row.posted_date;
  const amount = formatMoney(row.amount.amount, row.amount.currency) ?? row.amount.amount;
  return `Open ${name}, ${date}, ${amount}`;
}

/** The sentence under the filters: what the rows shown add up to. */
function TotalLine({ rows }: { rows: readonly Transaction[] }) {
  const sum = totals(rows);
  if (sum.count === 0) {
    return null;
  }
  const money = (amount: string, currency: string): string => formatMoney(amount, currency) ?? amount;
  const counted = sum.count - sum.transfers;
  return (
    <div className="tx-totals">
      {sum.currency !== null ? (
        <p className="tx-totals__sum">
          {`In ${money(sum.in, sum.currency)} · Out ${money(sum.out, sum.currency)} · Net ${money(sum.net, sum.currency)}, of the ${String(sum.count)} shown`}
        </p>
      ) : counted > 0 ? (
        <p className="tx-totals__sum">{`The ${String(sum.count)} shown are in more than one currency, so they are not added up.`}</p>
      ) : null}
      {sum.transfers === 0 ? null : (
        <p className="tx-totals__note">
          {sum.transfers === 1
            ? "1 transfer between your accounts is left out of in and out."
            : `${String(sum.transfers)} transfers between your accounts are left out of in and out.`}
        </p>
      )}
    </div>
  );
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
  // The address is the only place the filters live.
  const filters = fromSearch(search);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [splitting, setSplitting] = useState<Transaction | null>(null);
  const [transferring, setTransferring] = useState<Transaction | null>(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Transaction | null>(null);
  const [noting, setNoting] = useState<Transaction | null>(null);
  const [deleting, setDeleting] = useState<Transaction | null>(null);
  // The search text is typed here and written to the address on Enter; a new
  // `q` in the address (a link, Clear, the rail) replaces what was typed.
  const [typed, setTyped] = useState(filters.q);
  const [typedFor, setTypedFor] = useState(filters.q);
  if (typedFor !== filters.q) {
    setTypedFor(filters.q);
    setTyped(filters.q);
  }
  // The filters behind "More filters": open when the address sets one, and
  // counted on the summary while it is closed, so none is hidden silently.
  const hidden = [filters.dateFrom, filters.dateTo, filters.payee, filters.tag, filters.reviewState].filter((value) => value !== "").length;
  const [moreOpen, setMoreOpen] = useState(hidden > 0);
  // "Always categorise <merchant> as..." from the row menu, and the rule that was just saved there.
  const [always, setAlways] = useState<Transaction | null>(null);
  const [savedRule, setSavedRule] = useState<SavedRule | null>(null);
  // The drawer: the row the address names. `tx` wins over `always`.
  const txId = idParam(search, "tx");
  const alwaysId = idParam(search, "always");
  const drawerId = txId ?? alwaysId;
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
    queryFn: ({ pageParam }) => listFiltered(filters, pageParam),
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

  const setFilters = (next: ListFilters): void => {
    setSelected(new Set());
    void navigate({ to: "/transactions", search: (previous: Record<string, unknown>) => withFilters(previous, next), replace: true });
  };
  const change = (patch: Partial<ListFilters>): void => {
    setFilters({ ...filters, ...patch });
  };
  const submitSearch = (event: FormEvent): void => {
    event.preventDefault();
    change({ q: typed.trim() });
  };
  const counted = (label: string, count: number | undefined): string => (count === undefined ? label : `${label} (${String(count)})`);

  const editable = (row: Transaction): boolean => canWrite && accountsById.get(row.account_id)?.can_edit === true;
  const selectableRows = rows.filter(editable);
  const allSelected = selectableRows.length > 0 && selectableRows.every((row) => selected.has(row.id));
  const canAdd = canWrite && (accounts.data ?? []).some((account) => account.can_edit);

  // Roving focus over the rows' link buttons: one is in the tab order, the arrows move it.
  const gridRef = useRef<HTMLDivElement>(null);
  const activeRow = useRef<string | null>(null);
  useEffect(() => {
    // The row id on each link button, so the active row survives a refetch or more rows.
    const buttons: HTMLButtonElement[] = [];
    gridRef.current?.querySelectorAll<HTMLTableRowElement>("tbody tr").forEach((tr, index) => {
      const button = tr.querySelector<HTMLButtonElement>(ROW_LINK);
      const row = rows.at(index);
      if (button !== null && row !== undefined) {
        button.setAttribute("data-row", row.id);
        buttons.push(button);
      }
    });
    const active = buttons.find((button) => button.getAttribute("data-row") === activeRow.current) ?? buttons[0];
    for (const button of buttons) {
      button.tabIndex = button === active ? 0 : -1;
    }
  });
  const moveFocus = (to: HTMLButtonElement): void => {
    const buttons = gridRef.current?.querySelectorAll<HTMLButtonElement>(ROW_LINK) ?? [];
    for (const button of buttons) {
      button.tabIndex = button === to ? 0 : -1;
    }
    activeRow.current = to.getAttribute("data-row");
    to.focus();
  };
  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    const target = event.target;
    if ((event.key !== "ArrowDown" && event.key !== "ArrowUp") || !(target instanceof HTMLButtonElement) || !target.matches(ROW_LINK)) {
      return;
    }
    const buttons = Array.from(gridRef.current?.querySelectorAll<HTMLButtonElement>(ROW_LINK) ?? []);
    const index = buttons.indexOf(target);
    const next = buttons[event.key === "ArrowDown" ? Math.min(index + 1, buttons.length - 1) : Math.max(index - 1, 0)];
    event.preventDefault();
    if (next !== undefined) {
      moveFocus(next);
    }
  };
  const openRow = (row: Transaction): void => {
    activeRow.current = row.id;
    void navigate({ to: "/transactions", search: (previous: Record<string, unknown>) => ({ ...withoutDrawer(previous), tx: row.id }) });
  };
  const closeDrawer = (): void => {
    void navigate({ to: "/transactions", search: (previous: Record<string, unknown>) => withoutDrawer(previous), replace: true });
  };
  // When the drawer closes, focus goes back to its row (also when it was opened from a link).
  const lastDrawer = useRef<string | null>(drawerId);
  useEffect(() => {
    const closed = lastDrawer.current;
    lastDrawer.current = drawerId;
    if (drawerId !== null || closed === null) {
      return;
    }
    const button = gridRef.current?.querySelector<HTMLButtonElement>(`${ROW_LINK}[data-row="${CSS.escape(closed)}"]`);
    if (button !== null && button !== undefined) {
      moveFocus(button);
    }
  });
  /** A deleted row leaves the selection, so no bulk action sends it. */
  const unselect = (id: string): void => {
    setSelected((current) => {
      const next = new Set(current);
      next.delete(id);
      return next;
    });
  };
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
    // A category is changed in the drawer.
    ...(row.splits.length === 0
      ? [
          {
            id: "category",
            label: "Change category…",
            onSelect: () => {
              openRow(row);
            },
          },
        ]
      : []),
    // A merchant rule is for members and above, which is everyone who has this menu.
    ...(canTeach(row)
      ? [
          {
            id: "always",
            label: `Always categorise ${merchantName(row) ?? "this merchant"} as…`,
            onSelect: () => {
              setAlways(row);
            },
          },
        ]
      : []),
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
      label: row.transfer === null ? "Mark as transfer" : "Unlink transfer",
      onSelect: () => {
        setTransferring(row);
      },
    },
    {
      id: "note",
      label: (row.notes ?? "") === "" ? "Add note…" : "Edit note…",
      onSelect: () => {
        setNoting(row);
      },
    },
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
            className: "tx-col-select",
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
    { id: "date", header: "Date", rowHeader: true, className: "tx-nowrap tx-col-date", cell: (row) => formatDate(row.posted_date) ?? row.posted_date },
    { id: "merchant", header: "Merchant", className: "tx-col-merchant", cell: (row) => <MerchantCell row={row} payees={payeeList} tags={tagList} /> },
    {
      id: "category",
      header: "Category",
      className: "tx-col-category",
      cell: (row) => (
        <CategoryCell
          row={row}
          editable={editable(row)}
          label={rowLabel(row, payeeList)}
          categories={categoryList}
          ruleNames={ruleNames}
          categoriesFailed={categories.isError}
        />
      ),
    },
    { id: "account", header: "Account", className: "tx-col-account", cell: (row) => accountLabelOf(row.account_id) ?? "" },
    {
      id: "amount",
      header: "Amount",
      align: "end",
      className: "tx-nowrap tx-col-amount",
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
    <section className="ledger-stack tx-page">
      <PageHeader
        title="Transactions"
        description="Every account in one list. Open a row to see where it came from or change its category."
        {...(canAdd
          ? {
              actions: (
                <>
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
                </>
              ),
            }
          : {})}
      />
      <div className="tx-bar">
        <form role="search" aria-label="Transactions" className="tx-search" onSubmit={submitSearch}>
          <Input
            type="search"
            aria-label="Search transactions"
            placeholder="Search merchant or description"
            value={typed}
            maxLength={100}
            onChange={(event) => {
              setTyped(event.target.value);
            }}
          />
          <Button type="submit">Search</Button>
        </form>
        <div className="tx-filters" role="group" aria-label="Filters">
          <div className="tx-filters__chips">
            <Chip
              pressed={filters.suggested}
              onClick={() => {
                change({ suggested: !filters.suggested });
              }}
            >
              {counted("Suggestions", summary.data?.suggestions)}
            </Chip>
            <Chip
              pressed={filters.category === "none"}
              onClick={() => {
                change({ category: filters.category === "none" ? "" : "none" });
              }}
            >
              {counted("No category", summary.data?.uncategorised)}
            </Chip>
          </div>
          <Field label="Account" className="tx-filters__field">
            <Select
              placeholder="All accounts"
              options={(accounts.data ?? []).map((account) => ({ value: account.id, label: accountLabel(account) }))}
              value={filters.account}
              onChange={(event) => {
                change({ account: event.target.value });
              }}
            />
          </Field>
          <Field label="Category" className="tx-filters__field">
            <CategoryPicker
              placeholder="Any category"
              categories={categoryList}
              value={filters.category === "none" ? "" : filters.category}
              onChange={(event) => {
                change({ category: event.target.value });
              }}
            />
          </Field>
          <details
            className="tx-more"
            open={moreOpen}
            onToggle={(event) => {
              setMoreOpen(event.currentTarget.open);
            }}
          >
            <summary>{moreOpen || hidden === 0 ? "More filters" : `More filters (${String(hidden)})`}</summary>
            <div className="tx-more__fields">
              <Field label="From">
                <Input
                  type="date"
                  value={filters.dateFrom}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value === "" || isIsoDate(value)) {
                      change({ dateFrom: value });
                    }
                  }}
                />
              </Field>
              <Field label="To">
                <Input
                  type="date"
                  value={filters.dateTo}
                  onChange={(event) => {
                    const value = event.target.value;
                    if (value === "" || isIsoDate(value)) {
                      change({ dateTo: value });
                    }
                  }}
                />
              </Field>
              <Field label="Payee">
                <Select
                  placeholder="Any payee"
                  options={payeeList.map((payee) => ({ value: payee.id, label: payee.name }))}
                  value={filters.payee}
                  onChange={(event) => {
                    change({ payee: event.target.value });
                  }}
                />
              </Field>
              <Field label="Tag">
                <Select
                  placeholder="Any tag"
                  options={tagList.map((tag) => ({ value: tag.id, label: tag.name }))}
                  value={filters.tag}
                  onChange={(event) => {
                    change({ tag: event.target.value });
                  }}
                />
              </Field>
              <Field label="Review state">
                <Select
                  placeholder="Any state"
                  options={REVIEW_STATES}
                  value={filters.reviewState}
                  onChange={(event) => {
                    change({ reviewState: reviewState(event.target.value) });
                  }}
                />
              </Field>
            </div>
          </details>
          {hasFilters(filters) ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setFilters(NO_FILTERS);
              }}
            >
              Clear filters
            </Button>
          ) : null}
        </div>
      </div>
      {filters.document === "" && filters.subscription === "" && filters.merchant === "" ? null : (
        <ul className="tx-narrowed" aria-label="The list is narrowed to">
          {filters.document === "" ? null : (
            <li className="tx-narrowed__item">
              <Link to="/inbox/documents/$documentId" params={{ documentId: filters.document }}>
                From one file
              </Link>
              <Button
                variant="ghost"
                size="sm"
                aria-label="Remove the file filter"
                onClick={() => {
                  change({ document: "" });
                }}
              >
                Remove
              </Button>
            </li>
          )}
          {filters.subscription === "" ? null : (
            <li className="tx-narrowed__item">
              <Link to="/plan/subscriptions/$id" params={{ id: filters.subscription }}>
                Charges of one subscription
              </Link>
              <Button
                variant="ghost"
                size="sm"
                aria-label="Remove the subscription filter"
                onClick={() => {
                  change({ subscription: "" });
                }}
              >
                Remove
              </Button>
            </li>
          )}
          {filters.merchant === "" ? null : (
            <li className="tx-narrowed__item">
              <span>{`Merchant: ${filters.merchant}`}</span>
              <Button
                variant="ghost"
                size="sm"
                aria-label="Remove the merchant filter"
                onClick={() => {
                  change({ merchant: "" });
                }}
              >
                Remove
              </Button>
            </li>
          )}
        </ul>
      )}
      {canWrite ? null : <p className="ledger-muted">You have read-only access.</p>}
      {hasFilters(filters) && query.isSuccess ? <TotalLine rows={rows} /> : null}
      {savedRule === null ? null : (
        <RuleSavedNotice
          key={`${savedRule.result.rule.id}:${savedRule.result.rule.updated_at}`}
          saved={savedRule}
          onDone={() => {
            setSavedRule(null);
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
        <div ref={gridRef} className="tx-grid-wrap" onKeyDown={onGridKeyDown}>
          <Table
            caption="Transactions"
            captionHidden
            maxHeight="none"
            density="compact"
            className="tx-grid"
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            rowLink={(row) => ({
              // Every row opens its drawer; what may be changed there depends on the account.
              label: openLabel(row, displayName(row, payeeList) ?? rowLabel(row, payeeList)),
              onOpen: () => {
                openRow(row);
              },
            })}
            empty={hasFilters(filters) ? "No transactions match. Clear the filters or try another search." : "No transactions yet."}
          />
        </div>
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
      {noting === null ? null : (
        <NoteDialog
          row={noting}
          label={rowLabel(noting, payeeList)}
          onClose={() => {
            setNoting(null);
          }}
        />
      )}
      {deleting === null ? null : (
        <DeleteTransactionDialog
          transaction={deleting}
          label={rowLabel(deleting, payeeList)}
          accountName={accountLabelOf(deleting.account_id)}
          onDeleted={unselect}
          onClose={() => {
            setDeleting(null);
          }}
        />
      )}
      {always === null ? null : (
        <AlwaysCategoriseDialog
          transaction={always}
          categories={categoryList}
          onClose={() => {
            setAlways(null);
          }}
          onSaved={setSavedRule}
        />
      )}
      {drawerId === null ? null : (
        <TransactionDrawer
          key={drawerId}
          transactionId={drawerId}
          alwaysOpen={txId === null || txId === alwaysId}
          onClose={closeDrawer}
          onDeleted={unselect}
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
