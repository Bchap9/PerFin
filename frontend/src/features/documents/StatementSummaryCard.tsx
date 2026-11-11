// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * What a brokerage statement says about itself, per account of the document:
 * the summary tables it prints (the change in the account's value, its cash
 * flow, income and realized gains) and each section of its activity against
 * the total printed for it. Read by PerFin to check the rows it imports;
 * shown here as printed. Nothing is shown for a document without one.
 */
import { useQuery } from "@tanstack/react-query";

import { Badge } from "../../components/Badge";
import { Card } from "../../components/Card";
import { MoneyText } from "../../components/MoneyText";
import { formatDate } from "../../lib/dates";
import {
  docKeys,
  getDocumentSummary,
  type StatementSummary,
  type SummaryGroup,
} from "./api";

const GROUP_TITLES: Readonly<Record<SummaryGroup["group"], string>> = {
  account_value: "Change in account value",
  cash_flow: "Cash flow",
  income: "Income",
  realized: "Realized gains and losses",
};

const CHECK_WORDS: ReadonlyMap<string, string> = new Map(
  Object.entries({
    positions: "each holding's quantity × price is its value",
    holdings_total: "the holdings add up to their printed total",
    cash: "opening cash plus activity is the closing cash",
    total_value: "holdings plus cash is the account value",
    index_value: "the account value matches the document's list of accounts",
  }),
);

function Amount({
  amount,
  currency,
}: {
  amount: string | null;
  currency: string | null;
}) {
  if (amount === null) {
    return <span aria-label="not printed">–</span>;
  }
  return currency === null ? (
    <>{amount}</>
  ) : (
    <MoneyText amount={amount} currency={currency} />
  );
}

function title(statement: StatementSummary): string {
  const name = statement.name ?? "Account";
  return statement.last4 === null
    ? name
    : `${name} ending in ${statement.last4}`;
}

function Checks({ statement }: { statement: StatementSummary }) {
  if (statement.reconciliation === null) {
    return null;
  }
  const failed = new Set(statement.failed);
  return (
    <>
      <p>
        {statement.reconciliation === "ok" ? (
          <Badge tone="success">Adds up</Badge>
        ) : statement.reconciliation === "mismatch" ? (
          <Badge tone="warning">Does not add up</Badge>
        ) : (
          <Badge tone="neutral">Nothing to check against</Badge>
        )}
      </p>
      {statement.checks.length === 0 ? null : (
        <ul>
          {statement.checks.map((check) => (
            <li key={check}>
              {failed.has(check) ? "Does not hold: " : "Holds: "}
              {CHECK_WORDS.get(check) ?? check}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

function Statement({ statement }: { statement: StatementSummary }) {
  const currency = statement.currency;
  const year = statement.groups.some((group) =>
    group.lines.some((line) => line.year_to_date !== null),
  );
  return (
    <section aria-label={title(statement)} className="ledger-stack">
      <h3>{title(statement)}</h3>
      <dl>
        {statement.period_start === null || statement.period_end === null ? null : (
          <>
            <dt>Period</dt>
            <dd>
              {formatDate(statement.period_start) ?? statement.period_start} to{" "}
              {formatDate(statement.period_end) ?? statement.period_end}
            </dd>
          </>
        )}
        <dt>Account value</dt>
        <dd>
          <Amount amount={statement.total_value} currency={currency} />
        </dd>
        <dt>Holdings</dt>
        <dd>
          {statement.positions} (
          <Amount amount={statement.positions_value} currency={currency} />)
        </dd>
        <dt>Activity lines</dt>
        <dd>{statement.activity}</dd>
      </dl>
      <Checks statement={statement} />
      {statement.groups.map((group) => (
        <div
          key={group.group}
          className="mapping-grid"
          role="region"
          aria-label={`${GROUP_TITLES[group.group]}, ${title(statement)}`}
          tabIndex={0}
        >
          <table className="table table--compact">
            <caption>{GROUP_TITLES[group.group]}</caption>
            <thead>
              <tr>
                <th scope="col">As printed</th>
                <th scope="col">This period</th>
                {year ? <th scope="col">Year to date</th> : null}
              </tr>
            </thead>
            <tbody>
              {group.lines.map((line, index) => (
                <tr key={`${String(index)}-${line.label}`}>
                  <th scope="row">
                    {line.detail ? "\u2003" : ""}
                    {line.role === "opening" || line.role === "closing" ? (
                      <strong>{line.label}</strong>
                    ) : (
                      line.label
                    )}
                  </th>
                  <td>
                    <Amount amount={line.amount} currency={currency} />
                  </td>
                  {year ? (
                    <td>
                      <Amount amount={line.year_to_date} currency={currency} />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {statement.sections.length === 0 ? null : (
        <div
          className="mapping-grid"
          role="region"
          aria-label={`Activity sections, ${title(statement)}`}
          tabIndex={0}
        >
          <table className="table table--compact">
            <caption>Activity sections against their printed totals</caption>
            <thead>
              <tr>
                <th scope="col">Section</th>
                <th scope="col">Lines read</th>
                <th scope="col">Printed total</th>
                <th scope="col">Lines add up to</th>
                <th scope="col">Result</th>
              </tr>
            </thead>
            <tbody>
              {statement.sections.map((section, index) => (
                <tr key={`${String(index)}-${section.label}`}>
                  <th scope="row">{section.label}</th>
                  <td>{section.rows}</td>
                  <td>
                    <Amount amount={section.printed} currency={currency} />
                  </td>
                  <td>
                    <Amount amount={section.computed} currency={currency} />
                  </td>
                  <td>
                    {section.status === "ok"
                      ? "Adds up"
                      : section.status === "mismatch"
                        ? "Does not add up"
                        : "No total printed"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function StatementSummaryCard({ documentId }: { documentId: string }) {
  const summary = useQuery({
    queryKey: docKeys.summary(documentId),
    queryFn: () => getDocumentSummary(documentId),
    retry: false,
  });
  const statements = summary.data?.statements ?? [];
  if (statements.length === 0) {
    return null;
  }
  return (
    <Card title="Statement summary">
      <p className="ledger-muted">
        The summary tables this statement prints, as PerFin read them. They
        are used to check the lines it imports and are never imported as
        transactions themselves.
      </p>
      {statements.map((statement) => (
        <Statement key={statement.index} statement={statement} />
      ))}
    </Card>
  );
}
