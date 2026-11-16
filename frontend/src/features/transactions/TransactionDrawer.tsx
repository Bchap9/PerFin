// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * One transaction in a right-hand drawer: what it is, where it came from and
 * how its category was decided, then the category itself and, for a row that
 * can teach, "Always use <category> for <merchant>".
 *
 * The row is read by id (it need not be on a loaded page of the grid), seeded
 * from the grid when it is there. Choosing a category is the person's own
 * decision, shown at once (`useEditTransaction`), with Undo beside it in the
 * drawer. A suggestion stays a question ("Groceries?") with Accept; the
 * picker never shows it as the category. Only a person who may edit the
 * row's account sees the picker, the rule, "New category…" and the actions;
 * everyone else sees what the row is and why. The dialogs the drawer opens
 * are inside it, so they stay reachable while the drawer is modal.
 */
import "./transactions.css";

import { useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { CategoryPicker } from "../../components/CategoryPicker";
import { Chip } from "../../components/Chip";
import { Drawer } from "../../components/Drawer";
import { Field } from "../../components/Field";
import { Menu, type MenuItem } from "../../components/Menu";
import { MoneyText } from "../../components/MoneyText";
import { categoryColourClass } from "../../lib/categoryColour";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { useCanWrite, useAccounts, accountLabel } from "../accounts/api";
import { NewCategoryDialog } from "../categories/NewCategoryDialog";
import { AlwaysSection } from "./AlwaysSection";
import {
  getTransaction,
  txKeys,
  useCategories,
  usePayees,
  useRuleNames,
  type Category,
  type Transaction,
  type TransactionPage,
} from "./api";
import { displayName, NoteDialog, rowLabel } from "./cells";
import { DeleteTransactionDialog } from "./DeleteTransactionDialog";
import { useAcceptSuggestion, useEditTransaction } from "./optimistic";
import { confidenceText, isGuess, isSuggestion, sourceLabel } from "./provenance";
import { SplitEditor } from "./SplitEditor";
import { TransactionDialog } from "./TransactionDialog";
import { TransferDialog } from "./TransferDialog";

export interface TransactionDrawerProps {
  transactionId: string;
  /** Opened from `?always=`: the "Always use…" box starts ticked. */
  alwaysOpen?: boolean;
  onClose: () => void;
}

/** "Merchant list, 90%": who or what decided the category, and how sure a guess is. */
export function decidedBy(row: Transaction, ruleNames: ReadonlyMap<string, string> | undefined): string {
  const suggestion = isSuggestion(row);
  const ruleId = row.category_rule_id ?? null;
  const source =
    row.category_source === "rule" && ruleId !== null && ruleNames === undefined ? "Rule" : sourceLabel(row, ruleId === null ? undefined : ruleNames?.get(ruleId));
  const confidence = suggestion || isGuess(row.category_source) ? confidenceText(row.category_confidence) : null;
  const parts = [suggestion ? "Suggestion" : null, source, confidence].filter((part): part is string => part !== null);
  if (parts.length > 0) {
    return parts.join(", ");
  }
  return row.category_id === null ? "Nobody yet" : "Not recorded";
}

/** The top-level category `id` sits in (itself when it is top-level), for "New category…" to start inside. */
function groupOf(id: string | null, categories: readonly Category[]): string | undefined {
  const byId = new Map(categories.map((category) => [category.id, category]));
  let current = id === null ? undefined : byId.get(id);
  for (let depth = 0; current !== undefined && current.parent_id !== null && depth < 20; depth += 1) {
    const parent = byId.get(current.parent_id);
    if (parent === undefined) {
      break;
    }
    current = parent;
  }
  return current?.id;
}

/** The row as the grid has it, so the drawer shows at once while it is read by id. */
function fromGrid(queryClient: ReturnType<typeof useQueryClient>, id: string): Transaction | undefined {
  for (const [, data] of queryClient.getQueriesData<InfiniteData<TransactionPage, string | null>>({ queryKey: txKeys.lists })) {
    const row = data?.pages.flatMap((page) => page.items).find((item) => item.id === id);
    if (row !== undefined) {
      return row;
    }
  }
  return undefined;
}

type Open = "edit" | "split" | "transfer" | "note" | "delete" | "category" | null;

export function TransactionDrawer({ transactionId, alwaysOpen = false, onClose }: TransactionDrawerProps) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: txKeys.one(transactionId),
    queryFn: () => getTransaction(transactionId),
    placeholderData: () => fromGrid(queryClient, transactionId),
  });
  const row = query.data;
  const payees = usePayees();
  const name = row === undefined ? "Transaction" : (displayName(row, payees.data ?? []) ?? "Transaction");

  return (
    <Drawer open onClose={onClose} title={name}>
      {row !== undefined ? (
        <DrawerBody row={row} alwaysOpen={alwaysOpen} onClose={onClose} />
      ) : query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : (
        <p className="ledger-muted">Loading the transaction…</p>
      )}
    </Drawer>
  );
}

/** What a category change was, for its undo. */
interface Change {
  from: string | null;
  to: string | null;
}

function DrawerBody({ row, alwaysOpen, onClose }: { row: Transaction; alwaysOpen: boolean; onClose: () => void }) {
  const canWrite = useCanWrite();
  const accounts = useAccounts();
  const categories = useCategories();
  const payees = usePayees();
  const ruleNames = useRuleNames(row.category_source === "rule" && row.category_rule_id != null);
  const edit = useEditTransaction();
  const accept = useAcceptSuggestion();
  const [open, setOpen] = useState<Open>(null);
  const [change, setChange] = useState<Change | null>(null);
  const [undone, setUndone] = useState<string | null>(null);

  const categoryList = categories.data ?? [];
  const payeeList = payees.data ?? [];
  const account = (accounts.data ?? []).find((candidate) => candidate.id === row.account_id);
  const editable = canWrite && account?.can_edit === true;
  const label = rowLabel(row, payeeList);
  const categoryName = (id: string | null): string => (id === null ? "no category" : (categoryList.find((category) => category.id === id)?.name ?? "the category it had"));
  const suggested = isSuggestion(row) ? categoryList.find((category) => category.id === row.suggested_category_id) : undefined;
  const current = row.category_id === null ? undefined : categoryList.find((category) => category.id === row.category_id);
  const date = formatDate(row.posted_date) ?? row.posted_date;
  const group = groupOf(row.category_id, categoryList);

  const choose = (categoryId: string | null): void => {
    if (categoryId === row.category_id) {
      return;
    }
    const from = row.category_id;
    edit.mutate(
      { id: row.id, patch: { category_id: categoryId } },
      {
        onSuccess: () => {
          setUndone(null);
          setChange({ from, to: categoryId });
        },
      },
    );
  };

  const undo = (): void => {
    if (change === null) {
      return;
    }
    const back = change.from;
    edit.mutate(
      { id: row.id, patch: { category_id: back } },
      {
        onSuccess: () => {
          setChange(null);
          setUndone(back === null ? "The category is cleared again, as your choice." : `${categoryName(back)} is back, as your choice.`);
        },
      },
    );
  };

  const more: MenuItem[] = [
    {
      id: "transfer",
      label: row.transfer === null ? "Mark as transfer" : "Unlink transfer",
      onSelect: () => {
        setOpen("transfer");
      },
    },
    {
      id: "note",
      label: (row.notes ?? "") === "" ? "Add note…" : "Edit note…",
      onSelect: () => {
        setOpen("note");
      },
    },
    {
      id: "delete",
      label: "Delete",
      danger: true,
      onSelect: () => {
        setOpen("delete");
      },
    },
  ];

  return (
    <div className="tx-drawer">
      <p className="tx-drawer__headline">
        <MoneyText amount={row.amount.amount} currency={row.amount.currency} tone="signed" />
        <span className="tx-drawer__date">{date}</span>
      </p>
      <dl className="tx-drawer__facts">
        <div>
          <dt>Account</dt>
          <dd>
            <Link to="/accounts/$accountId" params={{ accountId: row.account_id }}>
              {account === undefined ? "Open the account" : accountLabel(account)}
            </Link>
          </dd>
        </div>
        <div>
          <dt>Bank wrote</dt>
          <dd className="tx-drawer__raw">{(row.raw_payee ?? "").trim() === "" ? "Nothing" : row.raw_payee}</dd>
        </div>
        <div>
          <dt>Came from</dt>
          <dd>
            {row.source_document_id == null ? (
              "Entered by hand"
            ) : (
              <Link to="/inbox/documents/$documentId" params={{ documentId: row.source_document_id }}>
                The file it was imported from
              </Link>
            )}
          </dd>
        </div>
        <div>
          <dt>Category by</dt>
          <dd>{decidedBy(row, ruleNames)}</dd>
        </div>
      </dl>

      <div className="tx-drawer__category">
        {row.splits.length > 0 ? (
          <p>{`Split over ${String(row.splits.length)} lines, each with its own category.`}</p>
        ) : (
          <>
            {suggested === undefined ? null : (
              <p className="cat-cell__choice">
                <span className="cat-cell__suggestion">{suggested.name}?</span>
                {editable ? (
                  <Button
                    size="sm"
                    aria-label={`Accept ${suggested.name}`}
                    loading={accept.isPending}
                    onClick={() => {
                      accept.mutate(row.id);
                    }}
                  >
                    Accept
                  </Button>
                ) : null}
              </p>
            )}
            {editable ? (
              <Field label="Category">
                <CategoryPicker
                  categories={categoryList}
                  value={row.category_id ?? ""}
                  onCreate={() => {
                    setOpen("category");
                  }}
                  onChange={(event) => {
                    choose(event.target.value === "" ? null : event.target.value);
                  }}
                />
              </Field>
            ) : suggested !== undefined ? null : (
              <p>
                {current === undefined ? (
                  <span className="cat-cell__none">Uncategorised</span>
                ) : (
                  <Chip colourClass={categoryColourClass(current.id, categoryList)}>{current.name}</Chip>
                )}
              </p>
            )}
            {change === null && undone === null ? null : (
              <div className="tx-notice">
                <p role="status" className="tx-notice__text">
                  {change === null
                    ? undone
                    : change.to === null
                      ? "No category is your choice for this transaction."
                      : `${categoryName(change.to)} is your choice for this transaction.`}
                </p>
                {change === null ? null : (
                  <span className="tx-notice__actions">
                    <Button size="sm" loading={edit.isPending} aria-label={`Undo: put back ${categoryName(change.from)}`} onClick={undo}>
                      Undo
                    </Button>
                  </span>
                )}
              </div>
            )}
          </>
        )}
        {editable ? (
          <AlwaysSection
            key={row.id}
            transaction={row}
            categoryId={row.category_id ?? row.suggested_category_id ?? ""}
            defaultChecked={alwaysOpen}
          />
        ) : null}
      </div>

      {editable ? (
        <div className="tx-drawer__actions">
          <Button
            onClick={() => {
              setOpen("edit");
            }}
          >
            Edit details
          </Button>
          <Button
            onClick={() => {
              setOpen("split");
            }}
          >
            {row.splits.length > 0 ? "Edit split" : "Split"}
          </Button>
          <Menu label={`More actions for ${label}`} items={more} />
        </div>
      ) : null}

      {open === "edit" ? (
        <TransactionDialog
          transaction={row}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
      {open === "split" ? (
        <SplitEditor
          transaction={row}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
      {open === "transfer" ? (
        <TransferDialog
          transaction={row}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
      {open === "note" ? (
        <NoteDialog
          row={row}
          label={label}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
      {open === "delete" ? (
        <DeleteTransactionDialog
          transaction={row}
          label={label}
          accountName={account === undefined ? undefined : accountLabel(account)}
          onDeleted={onClose}
          onClose={() => {
            setOpen(null);
          }}
        />
      ) : null}
      <NewCategoryDialog
        open={open === "category"}
        {...(group === undefined ? {} : { parentId: group })}
        onClose={() => {
          setOpen(null);
        }}
        onCreated={(category) => {
          choose(category.id);
        }}
      />
    </div>
  );
}
