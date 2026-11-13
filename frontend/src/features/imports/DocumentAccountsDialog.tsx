// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Set up accounts from this file": every account a document names, the ones
 * PerFin already knows, and an editable proposal for each new one. One click
 * creates the new accounts and imports the file into all of them
 * (`POST /documents/{id}/accounts/apply`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { formatDate } from "../../lib/dates";
import { queryRoots } from "../../lib/queryKeys";
import {
  ACCOUNT_TYPES,
  accountLabel,
  subtypeOptions,
  useAccountSubtypes,
  useAccounts,
  type Account,
} from "../accounts/api";
import {
  applyDocumentAccounts,
  documentAccountKeys,
  getDocumentAccounts,
  type AccountChoice,
  type DetectedAccount,
} from "./api";

type AccountType = Account["type"];

/** What the person decided for one detected account. */
export interface RowChoice {
  /** "create" a new account, "use" an existing one, or "skip" (leave it for later). */
  mode: "create" | "use" | "skip";
  accountId: string;
  name: string;
  /** "" until the person chooses: the file did not say what kind of account it is. */
  type: AccountType | "";
  /** The kind of account within the type ("" for none). */
  subtype: string;
  institution: string;
  currency: string;
  openingBalance: string;
}

export function initialChoice(found: DetectedAccount): RowChoice {
  const proposal = found.proposal;
  const mode =
    found.status === "new" || found.status === "conflict"
      ? "create"
      : found.status === "ambiguous"
        ? "use"
        : "skip";
  return {
    mode,
    accountId: found.status === "ambiguous" ? (found.candidates[0] ?? "") : "",
    name: proposal?.name ?? "",
    type: proposal?.type ?? "",
    subtype: proposal?.subtype ?? "",
    institution: proposal?.institution_name ?? "",
    currency: proposal?.currency ?? "",
    openingBalance: proposal?.opening_balance ?? "",
  };
}

/** The request body for the rows the person acted on; problems if a row is incomplete. */
export function buildChoices(
  found: readonly DetectedAccount[],
  rows: readonly RowChoice[],
): { items: AccountChoice[]; problems: string[] } {
  const items: AccountChoice[] = [];
  const problems: string[] = [];
  found.forEach((detected, index) => {
    const row = rows.at(index);
    if (row === undefined || row.mode === "skip") {
      return;
    }
    const label =
      detected.name ?? detected.last4 ?? `account ${String(index + 1)}`;
    if (row.mode === "use") {
      if (row.accountId === "") {
        problems.push(`Choose the account for ${label}.`);
        return;
      }
      items.push({
        target: detected.target,
        index: detected.index,
        account_id: row.accountId,
      });
      return;
    }
    const name = row.name.trim();
    const currency = row.currency.trim().toUpperCase();
    if (name === "") {
      problems.push(`Name the new account for ${label}.`);
      return;
    }
    if (currency !== "" && !/^[A-Z]{3}$/.test(currency)) {
      problems.push(`Use a three-letter currency for ${label}.`);
      return;
    }
    if (row.type === "") {
      problems.push(`Choose the account type for ${label}.`);
      return;
    }
    const opening = row.openingBalance.trim();
    const create: NonNullable<AccountChoice["create"]> = {
      type: row.type,
      ...(row.subtype === "" ? {} : { subtype: row.subtype }),
      name,
      include_in_net_worth: true,
      ...(currency === "" ? {} : { currency }),
      ...(detected.last4 === null ? {} : { number_last4: detected.last4 }),
      ...(opening !== "" && currency !== ""
        ? {
            opening_balance: { amount: opening, currency },
            ...(detected.proposal?.opening_balance_date
              ? { opening_balance_date: detected.proposal.opening_balance_date }
              : {}),
          }
        : {}),
    };
    items.push({
      target: detected.target,
      index: detected.index,
      create,
      ...(row.institution.trim() === ""
        ? {}
        : { institution_name: row.institution.trim() }),
    });
  });
  if (items.length === 0 && problems.length === 0) {
    problems.push("Nothing to do: choose or create at least one account.");
  }
  return { items, problems };
}

const STATUS: Readonly<
  Record<
    DetectedAccount["status"],
    { label: string; tone: "success" | "warning" | "neutral" | "danger" }
  >
> = {
  matched: { label: "Known account", tone: "success" },
  new: { label: "New account", tone: "warning" },
  ambiguous: { label: "Several match", tone: "warning" },
  conflict: { label: "Does not fit", tone: "danger" },
  done: { label: "Imported", tone: "neutral" },
};

function describe(found: DetectedAccount): string {
  const parts: string[] = [];
  if (found.name) {
    parts.push(found.name);
  }
  if (found.institution_name) {
    parts.push(found.institution_name);
  }
  if (found.last4) {
    parts.push(`ending ${found.last4}`);
  }
  if (found.period_start && found.period_end) {
    parts.push(
      `${formatDate(found.period_start) ?? found.period_start} to ${formatDate(found.period_end) ?? found.period_end}`,
    );
  }
  parts.push(`${String(found.transactions)} rows`);
  return parts.join(" · ");
}

function AccountRow({
  found,
  row,
  accounts,
  onChange,
}: {
  found: DetectedAccount;
  row: RowChoice;
  accounts: readonly Account[];
  onChange: (change: Partial<RowChoice>) => void;
}) {
  const status = STATUS[found.status];
  const subtypes = useAccountSubtypes();
  const kinds = row.type === "" ? [] : subtypeOptions(subtypes.data, row.type);
  const known = accounts.find((account) => account.id === found.account_id);
  const choices =
    found.status === "ambiguous"
      ? accounts.filter((account) => found.candidates.includes(account.id))
      : accounts;
  const editable = found.status !== "matched" && found.status !== "done";
  return (
    <li className="ledger-stack document-account">
      <div className="document-account__head">
        <Badge tone={status.tone}>{status.label}</Badge>{" "}
        <span>{describe(found)}</span>
      </div>
      {found.status === "matched" && known !== undefined ? (
        <p className="ledger-muted">Imports into {accountLabel(known)}.</p>
      ) : null}
      {found.proposal?.suggested_by_model === true ? (
        <p className="ledger-muted">
          <Badge tone="neutral">Local model</Badge> Some details below were
          suggested by the local model; check them.
        </p>
      ) : null}
      {editable ? (
        <>
          <Select
            aria-label={`What to do with ${found.name ?? found.last4 ?? "this account"}`}
            options={[
              { value: "create", label: "Create a new account" },
              { value: "use", label: "Use an existing account" },
              { value: "skip", label: "Leave it for later" },
            ]}
            value={row.mode}
            onChange={(event) => {
              const mode =
                event.target.value === "use"
                  ? "use"
                  : event.target.value === "skip"
                    ? "skip"
                    : "create";
              onChange({ mode });
            }}
          />
          {row.mode === "use" ? (
            <Field label="Account">
              <Select
                placeholder="Choose an account"
                options={choices.map((account) => ({
                  value: account.id,
                  label: accountLabel(account),
                }))}
                value={row.accountId}
                onChange={(event) => {
                  onChange({ accountId: event.target.value });
                }}
              />
            </Field>
          ) : null}
          {row.mode === "create" ? (
            <div className="ledger-filters">
              <Field label="Name" required>
                <Input
                  value={row.name}
                  maxLength={120}
                  onChange={(event) => {
                    onChange({ name: event.target.value });
                  }}
                />
              </Field>
              <Field
                label="Type"
                required
                hint={
                  row.type === ""
                    ? found.proposal?.type === null
                      ? "PerFin could not tell from this file whether it is a bank account or a card, so choose the type."
                      : "Choose what kind of account this is."
                    : undefined
                }
              >
                <Select
                  {...(row.type === "" ? { placeholder: "Choose a type" } : {})}
                  options={ACCOUNT_TYPES.filter(
                    (entry) =>
                      entry.type !== "manual_asset" &&
                      entry.type !== "manual_liability",
                  ).map(({ type, label }) => ({
                    value: type,
                    label,
                  }))}
                  value={row.type}
                  onChange={(event) => {
                    const found = ACCOUNT_TYPES.find(
                      (entry) => entry.type === event.target.value,
                    );
                    if (found !== undefined) {
                      onChange({ type: found.type, subtype: "" });
                    }
                  }}
                />
              </Field>
              {kinds.length > 0 ? (
                <Field label="Kind">
                  <Select
                    placeholder="Not set"
                    options={kinds}
                    value={row.subtype}
                    onChange={(event) => {
                      onChange({ subtype: event.target.value });
                    }}
                  />
                </Field>
              ) : null}
              <Field label="Institution">
                <Input
                  value={row.institution}
                  maxLength={120}
                  onChange={(event) => {
                    onChange({ institution: event.target.value });
                  }}
                />
              </Field>
              <Field label="Currency">
                <Input
                  value={row.currency}
                  maxLength={3}
                  onChange={(event) => {
                    onChange({ currency: event.target.value });
                  }}
                />
              </Field>
              {found.proposal?.opening_balance !== null &&
              found.proposal?.opening_balance !== undefined ? (
                <Field
                  label="Opening balance"
                  hint="From the statement; clear it to leave it out."
                >
                  <Input
                    value={row.openingBalance}
                    inputMode="decimal"
                    onChange={(event) => {
                      onChange({ openingBalance: event.target.value });
                    }}
                  />
                </Field>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
    </li>
  );
}

function AccountsForm({
  documentId,
  found,
  onDone,
}: {
  documentId: string;
  found: DetectedAccount[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const accounts = useAccounts();
  const [rows, setRows] = useState<RowChoice[]>(() => found.map(initialChoice));
  const [problems, setProblems] = useState<string[]>([]);
  const editable = (accounts.data ?? []).filter(
    (account) => account.can_edit && !account.is_closed,
  );
  const apply = useMutation({
    mutationFn: (items: AccountChoice[]) =>
      applyDocumentAccounts(documentId, items),
    onSuccess: (applied) => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      const created = applied.created.length;
      toast.notify({
        title:
          created > 0
            ? `${String(created)} account${created === 1 ? "" : "s"} created`
            : "Accounts chosen",
        description: "The file is being imported into them.",
        tone: "success",
      });
      onDone();
    },
    onError: (caught) => {
      setProblems([errorMessage(caught)]);
    },
  });
  const pending = found.some(
    (entry) => entry.status !== "matched" && entry.status !== "done",
  );
  return (
    <div className="ledger-stack">
      <ul className="document-accounts" aria-label="Accounts in this file">
        {found.map((entry, index) => (
          <AccountRow
            key={`${entry.target}-${String(entry.index)}`}
            found={entry}
            row={rows.at(index) ?? initialChoice(entry)}
            accounts={editable}
            onChange={(change) => {
              setRows((current) =>
                current.map((row, position) =>
                  position === index ? { ...row, ...change } : row,
                ),
              );
            }}
          />
        ))}
      </ul>
      {problems.length > 0 ? (
        <ul role="alert" className="ledger-error">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}
      {pending ? (
        <div className="ledger-actions">
          <Button
            variant="primary"
            loading={apply.isPending}
            onClick={() => {
              const built = buildChoices(found, rows);
              setProblems(built.problems);
              if (built.problems.length === 0) {
                apply.mutate(built.items);
              }
            }}
          >
            Create accounts and import
          </Button>
        </div>
      ) : (
        <p className="ledger-muted">
          Every account in this file is known to PerFin.
        </p>
      )}
    </div>
  );
}

export function DocumentAccountsDialog({
  documentId,
  onClose,
}: {
  documentId: string;
  onClose: () => void;
}) {
  const query = useQuery({
    queryKey: documentAccountKeys.one(documentId),
    queryFn: () => getDocumentAccounts(documentId),
  });
  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Accounts in this file"
      description="PerFin found these accounts in the file. Known ones import as before; create the new ones here and the file is imported into them."
    >
      {query.isPending ? <Spinner label="Reading the file's accounts" /> : null}
      {query.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(query.error)}
        </p>
      ) : null}
      {query.isSuccess && query.data.accounts.length === 0 ? (
        <p className="ledger-muted">
          This file names no accounts PerFin can read.
        </p>
      ) : null}
      {query.isSuccess && query.data.accounts.length > 0 ? (
        <AccountsForm
          documentId={documentId}
          found={query.data.accounts}
          onDone={onClose}
        />
      ) : null}
    </Dialog>
  );
}
