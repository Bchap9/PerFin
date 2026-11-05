// SPDX-License-Identifier: AGPL-3.0-or-later
import { useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { isCurrencyCode } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { createSecurity, type AssetClass, type Security } from "./api";
import { titleCase } from "./parts";

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

export function SecurityDialog({ defaultCurrency, onClose, onCreated }: { defaultCurrency: string; onClose: () => void; onCreated?: (security: Security) => void }) {
  const [symbol, setSymbol] = useState("");
  const [name, setName] = useState("");
  const [assetClass, setAssetClass] = useState<AssetClass>("equity");
  const [currency, setCurrency] = useState(defaultCurrency);
  const [sector, setSector] = useState("");
  const [problems, setProblems] = useState<Record<string, string>>({});
  const save = useAction(createSecurity, { invalidate: [queryRoots.investments], success: "Security added" });

  const submit = async (): Promise<void> => {
    const found: Record<string, string> = {};
    if (symbol.trim() === "") {
      found["symbol"] = "Enter the ticker symbol.";
    }
    if (name.trim() === "") {
      found["name"] = "Enter a name.";
    }
    if (!isCurrencyCode(currency.trim().toUpperCase())) {
      found["currency"] = "Enter a three-letter currency code.";
    }
    setProblems(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    const done = await save.run({
      symbol: symbol.trim().toUpperCase(),
      name: name.trim(),
      asset_class: assetClass,
      currency: currency.trim().toUpperCase(),
      ...(sector.trim() === "" ? {} : { sector: sector.trim() }),
    });
    if (done !== undefined) {
      onCreated?.(done.value);
      onClose();
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="New security"
      description="Securities are your own catalogue; nothing is looked up online."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Add security
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Symbol" error={problems["symbol"]} required>
          <Input value={symbol} onChange={(event) => { setSymbol(event.target.value); }} />
        </Field>
        <Field label="Name" error={problems["name"]} required>
          <Input value={name} onChange={(event) => { setName(event.target.value); }} />
        </Field>
        <Field label="Asset class">
          <Select value={assetClass} onChange={(event) => { setAssetClass(event.target.value as AssetClass); }} options={ASSET_CLASSES.map((value) => ({ value, label: titleCase(value) }))} />
        </Field>
        <Field label="Currency" error={problems["currency"]} required>
          <Input value={currency} maxLength={3} onChange={(event) => { setCurrency(event.target.value); }} />
        </Field>
        <Field label="Sector" hint="Optional. Used for the sector breakdown.">
          <Input value={sector} onChange={(event) => { setSector(event.target.value); }} />
        </Field>
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}
