// SPDX-License-Identifier: AGPL-3.0-or-later
import "../accounts/ledger.css";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts, useCanWrite } from "../accounts/api";
import { PageViewer } from "../documents/PageViewer";
import {
  applyMapping,
  createMapping,
  getDocument,
  getPreview,
  importKeys,
  type ImportPreview,
} from "./api";
import {
  DATE_FORMATS,
  buildSpec,
  hasAccountColumn,
  isColumnRole,
  isDateFormat,
  rolesFor,
  stateFromSpec,
  withMode,
  withRole,
  type MappingState,
} from "./mapping";

type PreviewTable = ImportPreview["tables"][number];
type Suggestion = NonNullable<ImportPreview["suggestion"]>;

const DOUBTS: ReadonlyMap<string, string> = new Map([
  [
    "header_not_exact",
    "Some column names only partly match what they were mapped to.",
  ],
  ["rows_unreadable", "Some rows could not be read with it."],
  [
    "date_order_guessed",
    "Every day in the file is 12 or less, so the day/month order is a guess.",
  ],
  ["no_rows", "No row could be read with it."],
  [
    "no_header",
    "The file has no header row, so the columns were worked out from the values alone.",
  ],
  [
    "indicator_values_unknown",
    "The debit/credit column has a value PerFin does not know, or the amounts beside it already carry a sign.",
  ],
  [
    "indicator_contradicts_sign",
    "The debit/credit words and the plus and minus signs of the amounts do not agree on every row.",
  ],
  [
    "sign_unverified",
    "Nothing in the file proves whether a positive amount is money in or money out. Find a row you recognise and check the sign.",
  ],
  [
    "investment_columns",
    "The file has a security, a quantity and a price column, so its rows look like trades, not money in and out.",
  ],
  [
    "activity_type_unknown",
    "Some rows have an action PerFin does not know yet; they are skipped and listed in the review queue.",
  ],
]);

/** What the server's guess is based on, so a person knows what to check. */
export function SuggestionNote({ suggestion }: { suggestion: Suggestion }) {
  const read = `${String(suggestion.parsed_rows)} rows read`;
  const failed =
    suggestion.failed_rows > 0
      ? `, ${String(suggestion.failed_rows)} could not be read`
      : "";
  const accounts =
    suggestion.accounts > 1
      ? ` across ${String(suggestion.accounts)} accounts`
      : "";
  const activity = suggestion.sign_evidence === "activity";
  const source = suggestion.spec.has_header
    ? "the header names"
    : "the values in them";
  return (
    <div className="ledger-muted" role="status" aria-label="Suggested mapping">
      <p>
        {activity
          ? "This looks like a brokerage activity export, so it is read as investment activity (buys, sells, dividends, cash). "
          : null}
        {suggestion.confident
          ? `PerFin filled in the columns from ${source}.`
          : `PerFin guessed the columns from ${source}; check them.`}{" "}
        ({read}
        {failed}
        {accounts}.)
      </p>
      {suggestion.doubts.length > 0 ? (
        <ul>
          {suggestion.doubts.map((doubt) => (
            <li key={doubt}>{DOUBTS.get(doubt) ?? doubt}</li>
          ))}
        </ul>
      ) : null}
      {suggestion.sign_evidence === null &&
      !activity &&
      !suggestion.doubts.includes("sign_unverified") ? (
        <p>
          Check the sign: nothing in the file proves whether positive amounts
          are money in or money out.
        </p>
      ) : null}
    </div>
  );
}

export function MappingForm({
  documentId,
  preview,
  table,
}: {
  documentId: string;
  preview: ImportPreview;
  table: PreviewTable;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const accounts = useAccounts();
  const [state, setState] = useState<MappingState>(() =>
    stateFromSpec(preview.suggested_spec, table.column_count, table.has_header),
  );
  const [name, setName] = useState("CSV import");
  const [accountId, setAccountId] = useState(preview.account_id ?? "");
  const [problems, setProblems] = useState<string[]>([]);
  const [serverError, setServerError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: async () => {
      const built = buildSpec(state);
      if (built.spec === null) {
        throw new Error(built.errors.join(" "));
      }
      const mapping = await createMapping({
        name: name.trim(),
        spec: built.spec,
        document_id: documentId,
        ...(accountId === "" ? {} : { account_id: accountId }),
      });
      return applyMapping(documentId, {
        mapping_id: mapping.id,
        ...(accountId === "" ? {} : { account_id: accountId }),
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      toast.notify({
        title: "Mapping saved",
        description: "The file is being imported.",
        tone: "success",
      });
      void navigate({ to: "/documents" });
    },
    onError: (error) => {
      setServerError(errorMessage(error));
    },
  });

  const submit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const built = buildSpec(state);
    const found = [...built.errors];
    if (name.trim() === "") {
      found.push("Name the mapping.");
    }
    if (
      accountId === "" &&
      preview.account_id === null &&
      !hasAccountColumn(state)
    ) {
      found.push("Choose the account these rows belong to.");
    }
    setProblems(found);
    setServerError(null);
    if (found.length === 0) {
      save.mutate();
    }
  };

  const patch = (change: Partial<MappingState>): void => {
    setState((current) => ({ ...current, ...change }));
  };
  const columns = Array.from(
    { length: table.column_count },
    (_, index) => index,
  );
  const editableAccounts = (accounts.data ?? []).filter(
    (account) => account.can_edit && !account.is_closed,
  );

  return (
    <form
      className="ledger-stack"
      noValidate
      aria-label="Column mapping"
      onSubmit={submit}
    >
      <Card title="Columns">
        <p className="ledger-muted">
          Tell PerFin what each column holds. Showing the first{" "}
          {table.rows.length} rows.
        </p>
        {preview.suggestion ? (
          <SuggestionNote suggestion={preview.suggestion} />
        ) : null}
        <Field
          label="The file holds"
          hint={
            state.mode === "activity"
              ? "Each row is a trade, dividend, reinvestment or cash movement; holdings and cost basis are kept per account."
              : "Each row is one transaction on a bank or card account."
          }
        >
          <Select
            options={[
              { value: "cash", label: "Bank or card transactions" },
              {
                value: "activity",
                label: "Investment activity (brokerage export)",
              },
            ]}
            value={state.mode}
            onChange={(event) => {
              const mode =
                event.target.value === "activity" ? "activity" : "cash";
              setState((current) => withMode(current, mode));
            }}
          />
        </Field>
        <div
          className="mapping-grid"
          role="region"
          aria-label="File preview"
          tabIndex={0}
        >
          <table className="table table--compact">
            <thead>
              <tr>
                {columns.map((index) => (
                  <th key={index} scope="col">
                    <Select
                      aria-label={`Column ${String(index + 1)} holds`}
                      options={rolesFor(state.mode)}
                      value={state.roles.at(index) ?? "ignore"}
                      onChange={(event) => {
                        if (isColumnRole(event.target.value)) {
                          const role = event.target.value;
                          setState((current) => ({
                            ...current,
                            roles: withRole(current.roles, index, role),
                          }));
                        }
                      }}
                    />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((index) => (
                    <td key={index}>{row.at(index) ?? ""}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="Format">
        <div className="ledger-filters">
          <Field label="Date format">
            <Select
              options={DATE_FORMATS.map((format) => ({
                value: format,
                label: format,
              }))}
              value={state.dateFormat}
              onChange={(event) => {
                if (isDateFormat(event.target.value)) {
                  patch({ dateFormat: event.target.value });
                }
              }}
            />
          </Field>
          <Field label="Decimal separator">
            <Select
              options={[
                { value: ".", label: "Point (1,234.50)" },
                { value: ",", label: "Comma (1.234,50)" },
              ]}
              value={state.decimalSeparator}
              onChange={(event) => {
                patch({
                  decimalSeparator: event.target.value === "," ? "," : ".",
                });
              }}
            />
          </Field>
          {state.mode === "cash" ? (
            <Field label="Positive amounts are">
              <Select
                options={[
                  { value: "inflow_positive", label: "Money in" },
                  { value: "outflow_positive", label: "Money out" },
                ]}
                value={state.sign}
                onChange={(event) => {
                  patch({
                    sign:
                      event.target.value === "outflow_positive"
                        ? "outflow_positive"
                        : "inflow_positive",
                  });
                }}
              />
            </Field>
          ) : null}
          <Field label="Rows to skip">
            <Input
              type="number"
              min={0}
              max={50}
              value={state.skipRows}
              onChange={(event) => {
                patch({ skipRows: Number(event.target.value) });
              }}
            />
          </Field>
          <Field label="Currency" hint="Used when no column holds it.">
            <Input
              value={state.currency}
              maxLength={3}
              onChange={(event) => {
                patch({ currency: event.target.value });
              }}
            />
          </Field>
          {state.mode === "cash" && state.roles.includes("indicator") ? (
            <Field
              label="Money-out indicator values"
              hint="Comma separated, e.g. DR, Debit."
            >
              <Input
                value={state.outflowValues}
                onChange={(event) => {
                  patch({ outflowValues: event.target.value });
                }}
              />
            </Field>
          ) : null}
        </div>
        <Checkbox
          label="The first row is a header"
          checked={state.hasHeader}
          onChange={(event) => {
            patch({ hasHeader: event.target.checked });
          }}
        />
      </Card>
      <Card title="Save">
        <div className="ledger-filters">
          <Field label="Mapping name" required>
            <Input
              value={name}
              maxLength={120}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
          </Field>
          <Field
            label="Account"
            hint={
              hasAccountColumn(state)
                ? "Each account number in the file is matched to your account with the same last four digits; an account chosen here takes the rows that match none."
                : undefined
            }
          >
            <Select
              placeholder={
                hasAccountColumn(state)
                  ? "Match by account number"
                  : preview.account_id === null
                    ? "Choose an account"
                    : "Keep the document's account"
              }
              options={editableAccounts.map((account) => ({
                value: account.id,
                label: accountLabel(account),
              }))}
              value={accountId}
              onChange={(event) => {
                setAccountId(event.target.value);
              }}
            />
          </Field>
        </div>
        {problems.length > 0 ? (
          <ul role="alert" className="ledger-error">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        ) : null}
        {serverError === null ? null : (
          <p role="alert" className="ledger-error">
            {serverError}
          </p>
        )}
        <div className="ledger-actions">
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save mapping and import
          </Button>
          <Link to="/documents">Cancel</Link>
        </div>
      </Card>
    </form>
  );
}

export function MappingEditorPage() {
  const documentId = useParams({
    strict: false,
    select: (params: Record<string, string | undefined>) =>
      params["documentId"] ?? "",
  });
  const canWrite = useCanWrite();
  const preview = useQuery({
    queryKey: importKeys.preview(documentId),
    queryFn: () => getPreview(documentId),
  });
  const table = preview.data?.tables[0];
  const document = useQuery({
    queryKey: importKeys.document(documentId),
    queryFn: () => getDocument(documentId),
    retry: false,
  });
  const mime = document.data?.mime_detected ?? "";
  const hasPages = mime === "application/pdf" || mime.startsWith("image/");
  return (
    <section aria-labelledby="page-title" className="ledger-stack">
      <div className="page-header">
        <h1 id="page-title">Map columns</h1>
        <Link to="/documents">All uploads</Link>
      </div>
      {preview.isPending ? <Spinner label="Loading preview" /> : null}
      {preview.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(preview.error)}
        </p>
      ) : null}
      {preview.isSuccess && table === undefined ? (
        <p className="ledger-muted">
          This file has no table to map. {preview.data.warnings.join(" ")}
        </p>
      ) : null}
      {preview.isSuccess && table !== undefined && !canWrite ? (
        <p className="ledger-muted">You have read-only access.</p>
      ) : null}
      {preview.isSuccess && table !== undefined && canWrite ? (
        <>
          {hasPages ? (
            <Card title="The page the table came from">
              <PageViewer documentId={documentId} />
            </Card>
          ) : null}
          <MappingForm
            documentId={documentId}
            preview={preview.data}
            table={table}
          />
        </>
      ) : null}
    </section>
  );
}
