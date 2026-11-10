// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { EmptyState } from "../../components/EmptyState";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { MoneyText } from "../../components/MoneyText";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { formatDate, isIsoDate, todayIsoDate } from "../../lib/dates";
import { amountSign, isDecimalString } from "../../lib/money";
import { queryRoots } from "../../lib/queryKeys";
import { useCanWrite } from "../accounts/api";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { deletePrice, investmentKeys, listPrices, listSecurities, setPrice, type Price, type Security } from "./api";
import { securityOptionLabel } from "./format";
import { DeleteSecurityDialog, SecurityDialog } from "./SecurityDialog";

/** Manual closing prices per security; a price replaces the one for that day. */
export function PricesTab({ currency }: { currency: string }) {
  const securities = useQuery({ queryKey: investmentKeys.securities, queryFn: listSecurities });
  const [chosen, setChosen] = useState("");
  const [adding, setAdding] = useState(false);
  // The security being edited or deleted is held as chosen, so a refreshed list cannot swap it under the dialog.
  const [editing, setEditing] = useState<Security | null>(null);
  const [deleting, setDeleting] = useState<Security | null>(null);
  const canWrite = useCanWrite();
  // The chosen security, or the first one (also once the chosen one has been deleted).
  const security = securities.data?.find((entry) => entry.id === chosen) ?? securities.data?.[0];
  const securityId = security?.id ?? "";
  const prices = useQuery({ queryKey: investmentKeys.prices(securityId), queryFn: () => listPrices(securityId), enabled: securityId !== "" });
  const [date, setDate] = useState(todayIsoDate());
  const [close, setClose] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const invalidate = [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard];
  const save = useAction((body: { price_date: string; close: string }) => setPrice(securityId, body), { invalidate, success: "Price saved" });
  const remove = useAction((id: string) => deletePrice(securityId, id), { invalidate, success: "Price deleted" });

  const columns: TableColumn<Price>[] = [
    { id: "date", header: "Date", rowHeader: true, cell: (p) => formatDate(p.price_date) ?? p.price_date },
    { id: "close", header: "Close", align: "end", cell: (p) => <MoneyText amount={p.close} currency={p.currency} tone="none" fractionDigits={4} /> },
    { id: "source", header: "Source", cell: (p) => p.source },
    {
      id: "remove",
      header: <span className="visually-hidden">Delete</span>,
      align: "end",
      cell: (p) => (
        <Button size="sm" variant="danger" aria-label={`Delete the price of ${p.price_date}`} onClick={() => void remove.run(p.id)}>
          Delete
        </Button>
      ),
    },
  ];

  return (
    <div className="planning-stack">
      <QueryState loading={securities.isPending} error={securities.error} label="securities">
        {(securities.data ?? []).length === 0 ? (
          <EmptyState
            title="No securities yet"
            description="Add the securities you hold, then enter their prices here."
            action={<Button variant="primary" onClick={() => { setAdding(true); }}>New security</Button>}
          />
        ) : (
          <Card title="Manual prices" headingLevel={2} actions={<Button onClick={() => { setAdding(true); }}>New security</Button>}>
            <div className="planning-form">
              <div className="planning-row">
                <Field label="Security">
                  <Select
                    value={securityId}
                    onChange={(event) => { setChosen(event.target.value); }}
                    options={(securities.data ?? []).map((entry) => ({ value: entry.id, label: securityOptionLabel(entry) }))}
                  />
                </Field>
                {canWrite && security !== undefined ? (
                  <>
                    <Button onClick={() => { setEditing(security); }}>Edit security</Button>
                    <Button variant="danger" onClick={() => { setDeleting(security); }}>Delete security</Button>
                  </>
                ) : null}
              </div>
              <form
                className="planning-row"
                aria-label="Add a price"
                onSubmit={(event) => {
                  event.preventDefault();
                  if (!isIsoDate(date)) {
                    setProblem("Enter a valid date.");
                    return;
                  }
                  if (!isDecimalString(close) || amountSign(close) <= 0) {
                    setProblem("Enter a price above zero.");
                    return;
                  }
                  setProblem(null);
                  void save.run({ price_date: date, close }).then((done) => {
                    if (done !== undefined) {
                      setClose("");
                    }
                  });
                }}
              >
                <Field label="Date">
                  <Input type="date" value={date} onChange={(event) => { setDate(event.target.value); }} />
                </Field>
                <Field label={`Closing price (${security?.currency ?? currency})`}>
                  <Input inputMode="decimal" value={close} onChange={(event) => { setClose(event.target.value); }} />
                </Field>
                <Button type="submit" variant="primary" loading={save.busy}>
                  Save price
                </Button>
              </form>
              <ErrorLine message={problem ?? save.error ?? remove.error} />
              <QueryState loading={prices.isPending && securityId !== ""} error={prices.error} label="prices">
                <Table caption="Prices" captionHidden columns={columns} rows={prices.data ?? []} rowKey={(p) => p.id} maxHeight="md" empty="No prices recorded." />
              </QueryState>
            </div>
          </Card>
        )}
      </QueryState>
      {adding ? <SecurityDialog defaultCurrency={currency} onClose={() => { setAdding(false); }} onCreated={(created) => { setChosen(created.id); }} /> : null}
      {editing === null ? null : <SecurityDialog defaultCurrency={currency} security={editing} onClose={() => { setEditing(null); }} />}
      {deleting === null ? null : <DeleteSecurityDialog security={deleting} onClose={() => { setDeleting(null); }} onDeleted={() => { setChosen(""); }} />}
    </div>
  );
}
