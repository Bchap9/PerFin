// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { createSecurity, deleteSecurity, investmentKeys, updateSecurity, type AssetClass, type Security } from "./api";
import { securityLabel } from "./format";
import { titleCase } from "./parts";
import { buildSecurityCreate, buildSecurityUpdate, SECURITY_LIMITS, securityFormOf, type SecurityForm, type SecurityProblems } from "./securityForm";

export const ASSET_CLASSES: readonly AssetClass[] = [
  "equity",
  "etf",
  "mutual_fund",
  "bond",
  "cash_equivalent",
  "option",
  "crypto",
  "real_estate",
  "commodity",
  "other",
];

const ASSET_CLASS_OPTIONS = ASSET_CLASSES.map((value) => ({ value, label: titleCase(value) }));

/** Add a security to the catalogue, or (with `security`) correct the one given. */
export function SecurityDialog({
  defaultCurrency,
  security,
  onClose,
  onCreated,
}: {
  defaultCurrency: string;
  /** Present: edit this security; absent: create one. */
  security?: Security;
  onClose: () => void;
  onCreated?: (security: Security) => void;
}) {
  const [form, setForm] = useState<SecurityForm>(() => securityFormOf(security, defaultCurrency));
  const [problems, setProblems] = useState<SecurityProblems>({});
  const create = useAction(createSecurity, { invalidate: [queryRoots.investments], success: "Security added" });
  const update = useAction(updateSecurity, { invalidate: [queryRoots.investments], success: "Security updated" });
  const editing = security !== undefined;
  const set = <K extends keyof SecurityForm>(key: K, value: SecurityForm[K]): void => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const submit = async (): Promise<void> => {
    if (security === undefined) {
      const result = buildSecurityCreate(form);
      if (!result.ok) {
        setProblems(result.problems);
        return;
      }
      setProblems({});
      const done = await create.run(result.body);
      if (done !== undefined) {
        onCreated?.(done.value);
        onClose();
      }
      return;
    }
    const result = buildSecurityUpdate(form, security);
    if (!result.ok) {
      setProblems(result.problems);
      return;
    }
    setProblems({});
    if (Object.keys(result.body).length === 0) {
      onClose(); // nothing changed, nothing to send
      return;
    }
    const done = await update.run(security.id, result.body);
    if (done !== undefined) {
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={editing ? "Edit security" : "New security"}
      description={
        editing
          ? "Corrections show wherever this security appears: holdings, transactions and prices."
          : "Securities are your own catalogue; nothing is looked up online."
      }
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={create.busy || update.busy} onClick={() => void submit()}>
            {editing ? "Save changes" : "Add security"}
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field
          label="Symbol"
          error={problems.symbol}
          required={security?.symbol !== null}
          hint={editing && security.symbol === null ? "This security came from a statement by name only. Add its ticker symbol if it has one." : undefined}
        >
          <Input value={form.symbol} maxLength={SECURITY_LIMITS.symbol} onChange={(event) => { set("symbol", event.target.value); }} />
        </Field>
        <Field label="Name" error={problems.name} required>
          <Input value={form.name} maxLength={SECURITY_LIMITS.name} onChange={(event) => { set("name", event.target.value); }} />
        </Field>
        <Field label="Asset class">
          <Select value={form.assetClass} onChange={(event) => { set("assetClass", event.target.value as AssetClass); }} options={ASSET_CLASS_OPTIONS} />
        </Field>
        <Field label="Currency" error={problems.currency} required hint={editing ? "Can change only while the security has no prices or transactions." : undefined}>
          <Input value={form.currency} maxLength={3} onChange={(event) => { set("currency", event.target.value); }} />
        </Field>
        <Field label="Exchange" hint="Optional. Tells apart the same symbol listed on two exchanges.">
          <Input value={form.exchange} maxLength={SECURITY_LIMITS.exchange} onChange={(event) => { set("exchange", event.target.value); }} />
        </Field>
        <Field label="Sector" hint="Optional. Used for the sector breakdown.">
          <Input value={form.sector} maxLength={SECURITY_LIMITS.sector} onChange={(event) => { set("sector", event.target.value); }} />
        </Field>
        <Field label="ISIN" error={problems.isin} hint="Optional.">
          <Input value={form.isin} maxLength={SECURITY_LIMITS.isin} onChange={(event) => { set("isin", event.target.value); }} />
        </Field>
        <Field label="CUSIP" error={problems.cusip} hint="Optional.">
          <Input value={form.cusip} maxLength={SECURITY_LIMITS.cusip} onChange={(event) => { set("cusip", event.target.value); }} />
        </Field>
        <ErrorLine message={create.error ?? update.error} />
      </div>
    </Dialog>
  );
}

/** Confirm deleting a security; the server refuses one that has transactions or lots. */
export function DeleteSecurityDialog({ security, onClose, onDeleted }: { security: Security; onClose: () => void; onDeleted?: () => void }) {
  const queryClient = useQueryClient();
  const remove = useAction(
    async (id: string) => {
      await deleteSecurity(id);
      // Its prices went with it: drop them so the refresh below does not ask for them again.
      queryClient.removeQueries({ queryKey: investmentKeys.prices(id) });
    },
    { invalidate: [queryRoots.investments], success: "Security deleted" },
  );
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Delete ${securityLabel(security)}?`}
      description="This removes the security and the prices recorded for it. A security that has transactions or lots cannot be deleted."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            loading={remove.busy}
            onClick={() => {
              void remove.run(security.id).then((done) => {
                if (done !== undefined) {
                  onDeleted?.();
                  onClose();
                }
              });
            }}
          >
            Delete security
          </Button>
        </>
      }
    >
      <ErrorLine message={remove.error} />
    </Dialog>
  );
}
