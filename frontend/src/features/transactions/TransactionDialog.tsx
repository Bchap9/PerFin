// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Add a transaction by hand, or edit one (date, amount, payee, description,
 * category, memo, status). The amount is typed as a positive number with a
 * "money out / money in" choice. The account of an existing transaction never
 * changes, and the server refuses a new amount on a split transaction or a
 * transfer leg (`409`), so the amount is locked there with the reason.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage, isApiError } from "../../lib/api/client";
import { isIsoDate, todayIsoDate } from "../../lib/dates";
import { accountLabel, useAccounts, type Account } from "../accounts/api";
import {
  createTransaction,
  invalidateLedger,
  newIdempotencyKey,
  updateTransaction,
  useCategories,
  usePayees,
  type Transaction,
  type TransactionCreate,
  type TransactionStatus,
  type TransactionUpdate,
} from "./api";
import { amountLock, createBody, parsePositiveAmount, unsignedAmount, updateBody, type Direction, type TransactionForm } from "./form";

const STATUSES: readonly { value: TransactionStatus; label: string }[] = [
  { value: "cleared", label: "Cleared" },
  { value: "pending", label: "Pending" },
  { value: "reconciled", label: "Reconciled" },
];

function isStatus(value: string): value is TransactionStatus {
  return STATUSES.some((status) => status.value === value);
}

type FieldName = "account" | "date" | "amount" | "payee" | "description" | "category" | "memo" | "status";

/** Where a validation problem from the server (`errors[].loc`) belongs in the form. */
const SERVER_FIELDS: ReadonlyMap<string, FieldName> = new Map([
  ["account_id", "account"],
  ["posted_date", "date"],
  ["amount", "amount"],
  ["amount.amount", "amount"],
  ["amount.currency", "amount"],
  ["payee_id", "payee"],
  ["raw_payee", "description"],
  ["category_id", "category"],
  ["memo", "memo"],
  ["status", "status"],
]);

/** The field that shows a problem with each part of the form. */
const FORM_FIELDS: ReadonlyMap<keyof TransactionForm, FieldName> = new Map([
  ["accountId", "account"],
  ["date", "date"],
  ["direction", "amount"],
  ["amount", "amount"],
  ["payeeId", "payee"],
  ["description", "description"],
  ["categoryId", "category"],
  ["memo", "memo"],
  ["status", "status"],
]);

function initialForm(transaction: Transaction | undefined, defaultAccountId: string | undefined): TransactionForm {
  if (transaction === undefined) {
    return {
      accountId: defaultAccountId ?? "",
      date: todayIsoDate(),
      direction: "out",
      amount: "",
      payeeId: "",
      description: "",
      categoryId: "",
      memo: "",
      status: "cleared",
    };
  }
  return {
    accountId: transaction.account_id,
    date: transaction.posted_date,
    ...unsignedAmount(transaction.amount.amount),
    payeeId: transaction.payee_id ?? "",
    description: transaction.raw_payee ?? "",
    categoryId: transaction.category_id ?? "",
    memo: transaction.memo ?? "",
    status: transaction.status,
  };
}

function accountOption(account: Account): { value: string; label: string } {
  return { value: account.id, label: account.is_closed ? `${accountLabel(account)} (closed)` : accountLabel(account) };
}

type SaveVars = { kind: "create"; body: TransactionCreate } | { kind: "update"; id: string; patch: TransactionUpdate };

export interface TransactionDialogProps {
  /** Present: edit this transaction; absent: add one. */
  transaction?: Transaction;
  /** When adding: the account to start on (ignored unless the person may edit it). */
  defaultAccountId?: string;
  onClose: () => void;
}

export function TransactionDialog({ transaction, defaultAccountId, onClose }: TransactionDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const accounts = useAccounts();
  const categories = useCategories();
  const payees = usePayees();
  const formId = useId();
  const directionName = useId();
  const firstControl = useRef<HTMLSelectElement | HTMLInputElement | null>(null);
  const editing = transaction !== undefined;
  const [initial] = useState(() => initialForm(transaction, defaultAccountId));
  const [form, setForm] = useState<TransactionForm>(initial);
  const [errors, setErrors] = useState<ReadonlyMap<FieldName, string>>(new Map());
  const [serverError, setServerError] = useState<string | null>(null);
  // One Idempotency-Key per distinct request: a retry of the same body reuses it, an edited body gets a new one.
  const idempotency = useRef<{ key: string; body: string } | null>(null);

  useEffect(() => {
    // The dialog itself focuses its close button; start people on the form instead.
    firstControl.current?.focus();
  }, []);

  const allAccounts = accounts.data ?? [];
  const editableAccounts = allAccounts.filter((item) => item.can_edit);
  // The chosen account; a household with a single account needs no choosing.
  const account = editing
    ? allAccounts.find((item) => item.id === form.accountId)
    : (editableAccounts.find((item) => item.id === form.accountId) ?? (editableAccounts.length === 1 ? editableAccounts[0] : undefined));
  const currency = account?.currency ?? transaction?.amount.currency ?? "";
  const lock = transaction === undefined ? null : amountLock(transaction);
  const split = transaction !== undefined && transaction.splits.length > 0;
  const payeeList = payees.data ?? [];
  // A household without payees only has descriptions to type.
  const showPayee = payeeList.length > 0 || form.payeeId !== "";

  const set = <K extends keyof TransactionForm>(key: K, value: TransactionForm[K]): void => {
    setForm((current) => ({ ...current, [key]: value }));
    // A field that is being corrected stops showing its old problem.
    const field = FORM_FIELDS.get(key);
    setErrors((current) => {
      if (field === undefined || !current.has(field)) {
        return current;
      }
      const next = new Map(current);
      next.delete(field);
      return next;
    });
  };

  const save = useMutation({
    mutationFn: (vars: SaveVars): Promise<Transaction> => {
      if (vars.kind === "update") {
        return updateTransaction(vars.id, vars.patch);
      }
      const body = JSON.stringify(vars.body);
      if (idempotency.current?.body !== body) {
        idempotency.current = { key: newIdempotencyKey(), body };
      }
      return createTransaction(vars.body, idempotency.current.key);
    },
    onSuccess: () => {
      invalidateLedger(queryClient);
      toast.notify({ title: editing ? "Transaction saved" : "Transaction added", tone: "success" });
      onClose();
    },
    onError: (error) => {
      const found = new Map<FieldName, string>();
      if (isApiError(error)) {
        for (const [path, message] of Object.entries(error.fieldErrors())) {
          const field = SERVER_FIELDS.get(path);
          if (field !== undefined) {
            found.set(field, message);
          }
        }
      }
      setErrors(found);
      setServerError(errorMessage(error));
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (save.isPending) {
      return;
    }
    const found = new Map<FieldName, string>();
    if (!editing && account === undefined) {
      found.set("account", "Choose an account.");
    }
    if (!isIsoDate(form.date)) {
      found.set("date", "Enter a date.");
    }
    // An amount the person did not touch is left alone (and a locked one cannot be touched).
    const amountUntouched = editing && form.amount.trim() === initial.amount && form.direction === initial.direction;
    let amount: string | null = null;
    if (lock === null && !amountUntouched) {
      const checked = parsePositiveAmount(form.amount, currency);
      if (checked.ok) {
        amount = checked.amount;
      } else {
        found.set("amount", checked.error);
      }
    }
    setErrors(found);
    setServerError(null);
    if (found.size > 0) {
      return;
    }
    if (transaction === undefined) {
      if (account !== undefined && amount !== null) {
        save.mutate({ kind: "create", body: createBody({ ...form, accountId: account.id }, amount, account.currency) });
      }
      return;
    }
    const patch = updateBody(transaction, form, amount);
    if (Object.keys(patch).length === 0) {
      onClose(); // nothing was changed
      return;
    }
    save.mutate({ kind: "update", id: transaction.id, patch });
  };

  const direction = (value: Direction, label: string, hint: string) => (
    <label className="tx-direction__option">
      <input
        type="radio"
        name={directionName}
        value={value}
        checked={form.direction === value}
        disabled={lock !== null}
        onChange={() => {
          set("direction", value);
        }}
      />
      <span>
        {label}
        <span className="tx-direction__hint">{hint}</span>
      </span>
    </label>
  );

  const noAccounts = !editing && accounts.isSuccess && editableAccounts.length === 0;

  return (
    <Dialog
      open
      onClose={onClose}
      title={editing ? "Edit transaction" : "Add transaction"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" form={formId} loading={save.isPending} disabled={noAccounts}>
            {editing ? "Save changes" : "Add transaction"}
          </Button>
        </>
      }
    >
      <form id={formId} className="tx-form" noValidate onSubmit={submit}>
        {noAccounts ? (
          <p role="alert" className="ledger-error tx-form__wide">
            You have no account you may add transactions to.
          </p>
        ) : null}
        <Field
          label="Account"
          required={!editing}
          error={errors.get("account")}
          hint={editing ? "A transaction stays on its account. To move it, delete it and add it to the other account." : undefined}
          className="tx-form__wide"
        >
          <Select
            ref={(node) => {
              if (!editing) {
                firstControl.current = node;
              }
            }}
            {...(editing || editableAccounts.length === 1 ? {} : { placeholder: "Choose an account" })}
            options={(editing ? allAccounts.filter((item) => item.id === form.accountId) : editableAccounts).map(accountOption)}
            value={account?.id ?? ""}
            disabled={editing}
            onChange={(event) => {
              set("accountId", event.target.value);
            }}
          />
        </Field>
        <Field label="Date" required error={errors.get("date")}>
          <Input
            ref={(node) => {
              if (editing) {
                firstControl.current = node;
              }
            }}
            type="date"
            value={form.date}
            onChange={(event) => {
              set("date", event.target.value);
            }}
          />
        </Field>
        <Field label="Status" error={errors.get("status")}>
          <Select
            options={STATUSES}
            value={form.status}
            onChange={(event) => {
              if (isStatus(event.target.value)) {
                set("status", event.target.value);
              }
            }}
          />
        </Field>
        <fieldset className="tx-direction tx-form__wide">
          <legend>Money out or money in</legend>
          {direction("out", "Money out", "Spending, a payment, a withdrawal")}
          {direction("in", "Money in", "Income, a refund, a deposit")}
        </fieldset>
        <Field
          label={currency === "" ? "Amount" : `Amount (${currency})`}
          required={lock === null}
          error={errors.get("amount")}
          hint={lock ?? "A positive number, such as 12.50."}
          className="tx-form__wide"
        >
          <Input
            value={form.amount}
            inputMode="decimal"
            autoComplete="off"
            maxLength={32}
            disabled={lock !== null}
            onChange={(event) => {
              set("amount", event.target.value);
            }}
          />
        </Field>
        {showPayee ? (
          <Field label="Payee" error={errors.get("payee")}>
            <Select
              placeholder="No payee"
              options={payeeList.map((payee) => ({ value: payee.id, label: payee.name }))}
              value={form.payeeId}
              onChange={(event) => {
                set("payeeId", event.target.value);
              }}
            />
          </Field>
        ) : null}
        <Field
          label="Description"
          error={errors.get("description")}
          hint={payeeList.length > 0 ? "Who it was paid to or came from, if not a payee in the list." : "Who it was paid to or came from."}
          {...(showPayee ? {} : { className: "tx-form__wide" })}
        >
          <Input
            value={form.description}
            maxLength={500}
            onChange={(event) => {
              set("description", event.target.value);
            }}
          />
        </Field>
        <Field
          label="Category"
          error={errors.get("category")}
          hint={split ? "This transaction is split; its categories are on the split lines." : undefined}
        >
          <Select
            placeholder={split ? `Split (${String(transaction.splits.length)})` : "Uncategorised"}
            options={(categories.data ?? []).map((category) => ({ value: category.id, label: category.name }))}
            value={split ? "" : form.categoryId}
            disabled={split}
            onChange={(event) => {
              set("categoryId", event.target.value);
            }}
          />
        </Field>
        <Field label="Memo" error={errors.get("memo")}>
          <Input
            value={form.memo}
            maxLength={1000}
            onChange={(event) => {
              set("memo", event.target.value);
            }}
          />
        </Field>
        {transaction?.source_document_id == null ? null : (
          <p className="ledger-muted tx-form__wide">This transaction was imported from a document. Changes here do not change the document.</p>
        )}
        {serverError === null ? null : (
          <p role="alert" className="ledger-error tx-form__wide">
            {serverError}
          </p>
        )}
      </form>
    </Dialog>
  );
}
