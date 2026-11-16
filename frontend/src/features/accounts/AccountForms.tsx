// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Create/edit and close dialogs for accounts. "Add account" also makes a
 * loan with its terms (one `POST /loans`, which opens the account) and a
 * manual asset with its first value (`POST /accounts`, then `POST /assets`).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { isIsoDate, todayIsoDate } from "../../lib/dates";
import { isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { createAsset, ASSET_KINDS, KIND_LABELS, type AssetKind } from "../assets/api";
import {
  assetCreateBody,
  AssetFields,
  checkAssetForm,
  emptyAssetForm,
  type AssetForm,
  type AssetProblems,
} from "../assets/NewAssetDialog";
import { labelOf } from "../investments/format";
import { createLoan } from "../loans/api";
import { buildLoanCreate, emptyLoanForm, type LoanAccountType, type LoanForm, type LoanProblems } from "../loans/loanForm";
import { LoanTermsFields } from "../loans/LoanTermsFields";
import {
  ACCOUNT_TYPES,
  closeAccount,
  createAccount,
  subtypeOptions,
  updateAccount,
  useAccountSubtypes,
  type Account,
  type AccountType,
} from "./api";

const TYPE_OPTIONS = ACCOUNT_TYPES.map(({ type, label }) => ({
  value: type,
  label,
}));

function isAccountType(value: string): value is AccountType {
  return ACCOUNT_TYPES.some((entry) => entry.type === value);
}

const KIND_OPTIONS = ASSET_KINDS.map((kind) => ({ value: kind, label: labelOf(KIND_LABELS, kind) }));

function isAssetKind(value: string): value is AssetKind {
  return ASSET_KINDS.some((kind) => kind === value);
}

function loanType(type: AccountType): LoanAccountType | null {
  return type === "loan" || type === "mortgage" || type === "line_of_credit" ? type : null;
}

/** What "Add account" made, and a part that could not be saved after the account was. */
interface Created {
  title: string;
  account: Account | null;
  partFailed: string | null;
}

export interface AccountFormDialogProps {
  open: boolean;
  onClose: () => void;
  /** Present: edit this account; absent: create. */
  account?: Account;
  onSaved?: (account: Account) => void;
}

export function AccountFormDialog({
  open,
  onClose,
  account,
  onSaved,
}: AccountFormDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const editing = account !== undefined;
  const [name, setName] = useState(account?.name ?? "");
  const [nickname, setNickname] = useState(account?.nickname ?? "");
  const [type, setType] = useState<AccountType>(account?.type ?? "checking");
  const [subtype, setSubtype] = useState(account?.subtype ?? "");
  const subtypes = useAccountSubtypes();
  const kinds = subtypeOptions(subtypes.data, type);
  const [currency, setCurrency] = useState(account?.currency ?? "USD");
  const [last4, setLast4] = useState(account?.number_last4 ?? "");
  const [opening, setOpening] = useState("");
  const [openingDate, setOpeningDate] = useState(todayIsoDate());
  const [inNetWorth, setInNetWorth] = useState(
    account?.include_in_net_worth ?? true,
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [assetKind, setAssetKind] = useState<AssetKind>("property");
  const [assetForm, setAssetForm] = useState<AssetForm>(() => emptyAssetForm(todayIsoDate()));
  const [assetProblems, setAssetProblems] = useState<AssetProblems>({});
  const [withTerms, setWithTerms] = useState(true);
  const [loanForm, setLoanForm] = useState<LoanForm>(() => emptyLoanForm(todayIsoDate()));
  const [loanProblems, setLoanProblems] = useState<LoanProblems>({});
  const asLoan = editing ? null : loanType(type);
  const termsNow = asLoan !== null && withTerms;
  const asAsset = !editing && type === "manual_asset";
  const code = currency.trim().toUpperCase();

  const mutation = useMutation({
    mutationFn: async (): Promise<Created> => {
      const common = {
        name: name.trim(),
        nickname: nickname.trim() === "" ? null : nickname.trim(),
        number_last4: last4.trim() === "" ? null : last4.trim(),
        include_in_net_worth: inNetWorth,
        subtype: subtype === "" ? null : subtype,
      };
      if (account !== undefined) {
        const saved = await updateAccount(account.id, { ...common, type });
        return { title: "Account updated", account: saved, partFailed: null };
      }
      if (asLoan !== null && withTerms) {
        const built = buildLoanCreate(loanForm, { name: name.trim(), type: asLoan, currency: code });
        if (!built.ok) {
          throw new Error("The loan terms are not complete.");
        }
        const loan = await createLoan(built.body);
        const extras =
          common.nickname !== null || common.number_last4 !== null || common.subtype !== null || !inNetWorth;
        if (!extras) {
          return { title: "Loan added", account: null, partFailed: null };
        }
        try {
          const saved = await updateAccount(loan.account_id, {
            nickname: common.nickname,
            number_last4: common.number_last4,
            subtype: common.subtype,
            include_in_net_worth: inNetWorth,
          });
          return { title: "Loan added", account: saved, partFailed: null };
        } catch (caught) {
          return {
            title: "Loan added",
            account: null,
            partFailed: `The loan was added, but its nickname, digits, kind or net worth choice were not saved: ${errorMessage(caught)} Edit the account to set them.`,
          };
        }
      }
      const saved = await createAccount({
        ...common,
        type,
        currency: code,
        ...(asAsset ? { asset_kind: assetKind } : {}),
        ...(asAsset || opening.trim() === ""
          ? {}
          : {
              opening_balance: { amount: opening.trim(), currency: code },
              opening_balance_date: openingDate,
            }),
      });
      if (asAsset) {
        try {
          await createAsset(assetCreateBody(assetForm, { id: saved.id, currency: saved.currency }));
        } catch (caught) {
          return {
            title: "Account created",
            account: saved,
            partFailed: `The account was created, but its asset details were not saved: ${errorMessage(caught)} Add them from the Accounts page.`,
          };
        }
      }
      return { title: "Account created", account: saved, partFailed: null };
    },
    onSuccess: (created) => {
      for (const root of [queryRoots.accounts, queryRoots.loans, queryRoots.assets, queryRoots.netWorth]) {
        void queryClient.invalidateQueries({ queryKey: root });
      }
      if (created.partFailed === null) {
        toast.notify({ title: created.title, tone: "success" });
      } else {
        toast.notify({ title: created.partFailed, tone: "error" });
      }
      if (created.account !== null) {
        onSaved?.(created.account);
      }
      onClose();
    },
    onError: (error) => {
      setErrors({ form: errorMessage(error) });
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (name.trim() === "") {
      found["name"] = "Enter a name.";
    }
    if (!editing) {
      if (!/^[A-Za-z]{3}$/.test(currency.trim())) {
        found["currency"] = "Use a three-letter currency code.";
      }
      if (!asAsset && !termsNow && opening.trim() !== "") {
        if (!isDecimalString(opening.trim())) {
          found["opening"] = "Enter an amount such as 1250.00.";
        }
        if (!isIsoDate(openingDate)) {
          found["openingDate"] = "Enter a date.";
        }
      }
    }
    if (last4.trim() !== "" && !/^\d{4}$/.test(last4.trim())) {
      found["last4"] = "Enter the last four digits.";
    }
    let detailsOk = true;
    if (asAsset) {
      const problems = checkAssetForm(assetForm);
      setAssetProblems(problems);
      detailsOk = Object.keys(problems).length === 0;
    }
    if (asLoan !== null && withTerms) {
      // The name and currency are checked above; here only the terms.
      const built = buildLoanCreate(loanForm, { name: name.trim() === "" ? "-" : name.trim(), type: asLoan, currency: "USD" });
      const problems = built.ok ? {} : built.problems;
      setLoanProblems(problems);
      detailsOk = detailsOk && built.ok;
    }
    setErrors(found);
    if (Object.keys(found).length === 0 && detailsOk) {
      mutation.mutate();
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={editing ? "Edit account" : "New account"}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            type="submit"
            form="account-form"
            loading={mutation.isPending}
          >
            {editing ? "Save changes" : "Create account"}
          </Button>
        </>
      }
    >
      <form
        id="account-form"
        className="ledger-stack"
        noValidate
        onSubmit={submit}
      >
        <Field label="Name" required error={errors["name"]}>
          <Input
            value={name}
            maxLength={120}
            onChange={(event) => {
              setName(event.target.value);
            }}
          />
        </Field>
        <Field label="Nickname">
          <Input
            value={nickname}
            maxLength={120}
            onChange={(event) => {
              setNickname(event.target.value);
            }}
          />
        </Field>
        <Field label="Type">
          <Select
            options={TYPE_OPTIONS}
            value={type}
            onChange={(event) => {
              if (isAccountType(event.target.value)) {
                setType(event.target.value);
                setSubtype(""); // each type has its own kinds
              }
            }}
          />
        </Field>
        {kinds.length > 0 ? (
          <Field
            label="Kind"
            hint="Optional: which kind of account it is (sets its tax treatment)."
          >
            <Select
              placeholder="Not set"
              options={kinds}
              value={subtype}
              onChange={(event) => {
                setSubtype(event.target.value);
              }}
            />
          </Field>
        ) : null}
        <Field
          label="Currency"
          error={errors["currency"]}
          hint={
            editing
              ? "The currency of an account cannot be changed here."
              : undefined
          }
        >
          <Input
            value={currency}
            maxLength={3}
            disabled={editing}
            onChange={(event) => {
              setCurrency(event.target.value);
            }}
          />
        </Field>
        <Field
          label="Last four digits"
          error={errors["last4"]}
          hint="Used to recognise statements of this account."
        >
          <Input
            value={last4}
            maxLength={4}
            inputMode="numeric"
            onChange={(event) => {
              setLast4(event.target.value);
            }}
          />
        </Field>
        {asAsset ? (
          <>
            <Field label="What it is">
              <Select
                options={KIND_OPTIONS}
                value={assetKind}
                onChange={(event) => {
                  if (isAssetKind(event.target.value)) {
                    setAssetKind(event.target.value);
                  }
                }}
              />
            </Field>
            <AssetFields
              form={assetForm}
              problems={assetProblems}
              currency={code === "" ? "USD" : code}
              onChange={(key, value) => {
                setAssetForm((current) => ({ ...current, [key]: value }));
              }}
            />
          </>
        ) : null}
        {asLoan === null ? null : (
          <Checkbox
            label="Record the loan terms now"
            hint="With its terms a loan gets a schedule, a payoff date and the extra payment simulator."
            checked={withTerms}
            onChange={(event) => {
              setWithTerms(event.target.checked);
            }}
          />
        )}
        {termsNow ? (
          <LoanTermsFields
            form={loanForm}
            problems={loanProblems}
            currency={code === "" ? "USD" : code}
            onChange={(key, value) => {
              setLoanForm((current) => ({ ...current, [key]: value }));
            }}
          />
        ) : null}
        {editing || asAsset || termsNow ? null : (
          <>
            <Field
              label="Opening balance"
              error={errors["opening"]}
              hint="Optional. Negative for money owed."
            >
              <Input
                value={opening}
                inputMode="decimal"
                onChange={(event) => {
                  setOpening(event.target.value);
                }}
              />
            </Field>
            <Field label="Opening balance date" error={errors["openingDate"]}>
              <Input
                type="date"
                value={openingDate}
                onChange={(event) => {
                  setOpeningDate(event.target.value);
                }}
              />
            </Field>
          </>
        )}
        <Checkbox
          label="Include in net worth"
          checked={inNetWorth}
          onChange={(event) => {
            setInNetWorth(event.target.checked);
          }}
        />
        {errors["form"] === undefined ? null : (
          <p role="alert" className="ledger-error">
            {errors["form"]}
          </p>
        )}
      </form>
    </Dialog>
  );
}

export function CloseAccountDialog({
  open,
  onClose,
  account,
}: {
  open: boolean;
  onClose: () => void;
  account: Account;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [closedOn, setClosedOn] = useState(todayIsoDate());
  const [error, setError] = useState<string | null>(null);
  const mutation = useMutation({
    mutationFn: () =>
      closeAccount(account.id, isIsoDate(closedOn) ? closedOn : null),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      toast.notify({ title: `${account.name} closed`, tone: "success" });
      onClose();
    },
    onError: (caught) => {
      setError(errorMessage(caught));
    },
  });
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Close ${account.name}?`}
      description="History stays. A closed account no longer counts toward today's balances and can be reopened."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={mutation.isPending}
            onClick={() => {
              mutation.mutate();
            }}
          >
            Close account
          </Button>
        </>
      }
    >
      <Field label="Closed on">
        <Input
          type="date"
          value={closedOn}
          onChange={(event) => {
            setClosedOn(event.target.value);
          }}
        />
      </Field>
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
    </Dialog>
  );
}
