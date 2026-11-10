// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The cost basis method of each investment account (first in, first out, or
 * average cost) and the dialog that changes it. The server replays every lot
 * of the account on a change, so the choice is confirmed in a dialog rather
 * than applied from the list.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useAccounts, type Account } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { getCostMethod, investmentKeys, setCostMethod, type CostMethod } from "./api";
import { labelOf } from "./format";
import { investmentAccounts } from "./TransactionsTab";

export const COST_METHOD_LABELS: Record<CostMethod, string> = {
  fifo: "First in, first out (FIFO)",
  average: "Average cost",
};

const COST_METHOD_EFFECT: Record<CostMethod, string> = {
  fifo: "A sale uses the cost of the oldest lots first.",
  average: "A sale uses the average cost of all units held, and the lots left afterwards carry that average.",
};

const METHOD_OPTIONS = (Object.keys(COST_METHOD_LABELS) as CostMethod[]).map((value) => ({ value, label: labelOf(COST_METHOD_LABELS, value) }));

const nameOf = (account: Account): string => account.nickname ?? account.name;

function CostMethodDialog({ account, current, onClose }: { account: Account; current: CostMethod; onClose: () => void }) {
  const [method, setMethod] = useState<CostMethod>(current);
  const save = useAction((next: CostMethod) => setCostMethod(account.id, next), {
    // Cost basis feeds gains everywhere they are shown; market value does not change.
    invalidate: [queryRoots.investments, queryRoots.reports, queryRoots.tax, queryRoots.dashboard],
    success: "Cost basis method changed",
  });
  const submit = async (): Promise<void> => {
    if (method === current) {
      onClose();
      return;
    }
    const done = await save.run(method);
    if (done !== undefined) {
      onClose();
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={`Cost basis method of ${nameOf(account)}`}
      description="Changing the method recalculates this account's lots, cost basis and realised gains from its first transaction."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.busy} onClick={() => void submit()}>
            Save method
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Method" hint={labelOf(COST_METHOD_EFFECT, method)}>
          <Select
            value={method}
            options={METHOD_OPTIONS}
            onChange={(event) => {
              setMethod(event.target.value === "average" ? "average" : "fifo");
            }}
          />
        </Field>
        {method === "average" && current !== "average" ? (
          <p className="muted">
            Positions that came from a statement without their purchases keep the averaged cost after a sale, even if you switch back later.
          </p>
        ) : null}
        <ErrorLine message={save.error} />
      </div>
    </Dialog>
  );
}

function CostMethodRow({ account }: { account: Account }) {
  const query = useQuery({ queryKey: investmentKeys.costMethod(account.id), queryFn: () => getCostMethod(account.id) });
  const [changing, setChanging] = useState(false);
  const name = nameOf(account);
  return (
    <li className="planning-list__item">
      <span>
        <strong>{name}</strong>{" "}
        {query.isPending ? (
          <Spinner size="sm" label={`Loading the cost basis method of ${name}`} />
        ) : query.isError ? (
          <span role="alert" className="auth-error">
            Could not load the method. {errorMessage(query.error)}
          </span>
        ) : (
          <span className="muted">{labelOf(COST_METHOD_LABELS, query.data.method)}</span>
        )}
      </span>
      {query.isSuccess && account.can_edit ? (
        <Button
          size="sm"
          aria-label={`Change the cost basis method of ${name}`}
          onClick={() => {
            setChanging(true);
          }}
        >
          Change
        </Button>
      ) : null}
      {changing && query.isSuccess ? (
        <CostMethodDialog
          account={account}
          current={query.data.method}
          onClose={() => {
            setChanging(false);
          }}
        />
      ) : null}
    </li>
  );
}

/** One row per open investment account (only `account` when one is chosen); nothing without any. */
export function CostMethodCard({ account }: { account: string }) {
  const accounts = useAccounts();
  const shown = investmentAccounts(accounts.data).filter((entry) => account === "" || entry.id === account);
  if (shown.length === 0) {
    return null;
  }
  return (
    <Card title="Cost basis method" headingLevel={2}>
      <div className="planning-form">
        <p className="muted">
          The method decides which purchase cost a sale is matched with, and so the cost basis and the realised and unrealised gains shown for the account.
        </p>
        <ul className="planning-list" aria-label="Cost basis method by account">
          {shown.map((entry) => (
            <CostMethodRow key={entry.id} account={entry} />
          ))}
        </ul>
      </div>
    </Card>
  );
}
