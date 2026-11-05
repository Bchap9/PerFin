// SPDX-License-Identifier: AGPL-3.0-or-later
/** Create/edit and close dialogs for accounts. */
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

  const mutation = useMutation({
    mutationFn: async (): Promise<Account> => {
      const common = {
        name: name.trim(),
        nickname: nickname.trim() === "" ? null : nickname.trim(),
        number_last4: last4.trim() === "" ? null : last4.trim(),
        include_in_net_worth: inNetWorth,
        subtype: subtype === "" ? null : subtype,
      };
      if (account !== undefined) {
        return updateAccount(account.id, { ...common, type });
      }
      return createAccount({
        ...common,
        type,
        currency: currency.trim().toUpperCase(),
        ...(opening.trim() === ""
          ? {}
          : {
              opening_balance: {
                amount: opening.trim(),
                currency: currency.trim().toUpperCase(),
              },
              opening_balance_date: openingDate,
            }),
      });
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.accounts });
      toast.notify({
        title: editing ? "Account updated" : "Account created",
        tone: "success",
      });
      onSaved?.(saved);
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
      if (opening.trim() !== "") {
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
    setErrors(found);
    if (Object.keys(found).length === 0) {
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
        {editing ? null : (
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
