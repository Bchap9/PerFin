// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Teach PerFin": show PerFin where the facts of a document are. The page is
 * shown; a click picks a word, a drag picks a box (or a line is picked from
 * a list, by keyboard). Each selection is then named from a list that always
 * ends with "Other — describe it" and "Ignore this"; a table gets a role per
 * column, a transaction table also what its amounts mean and whether it runs
 * on over the next pages. A preview shows what PerFin would read, and whether
 * it adds up, before anything is saved; only then can the labels be applied
 * to the document and, if asked, remembered for the next files of the same
 * layout (docs/INGESTION.md).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select } from "../../components/Select";
import { useToast } from "../../components/Toast";
import { errorMessage, isApiError } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { accountLabel, useAccounts } from "../accounts/api";
import { docKeys, getDocumentDetail } from "../documents/api";
import { PageViewer } from "../documents/PageViewer";
import {
  payloadString,
  teachDocument,
  type ReviewItem,
  type TeachOut,
  type TeachRequest,
} from "./api";
import {
  DIRECTION_CHOICES,
  SELECTION_CHOICES,
  isTable,
  keptText,
  newSelection,
  previewSummary,
  problems,
  readingKey,
  rolesForTable,
  selectionLabel,
  teachBody,
  warningText,
  type Selection,
  type SelectionKind,
  type TeachPreview,
  type TeachRole,
} from "./teach";

/** What PerFin read for each field so far (``null``: nothing). */
export type Readings = Partial<Record<SelectionKind, string | null>>;

/** Readings of a document and, for a review item, the figures it carries. */
export function currentReadings(
  document: { period_start?: string | null; period_end?: string | null } | null | undefined,
  item?: ReviewItem | null,
): Readings {
  const out: Readings = {
    period_start: document?.period_start ?? null,
    period_end: document?.period_end ?? null,
  };
  if (item !== null && item !== undefined) {
    out.opening_balance = payloadString(item, "opening");
    out.closing_balance = payloadString(item, "closing");
  }
  return out;
}

const SHOWN: readonly SelectionKind[] = [
  "period_start",
  "period_end",
  "opening_balance",
  "closing_balance",
];

export interface TeachDialogProps {
  documentId: string;
  /** The review item the teaching answers (closed when it is applied). */
  item?: ReviewItem | null;
  readings?: Readings;
  /** A name to start the institution field with. */
  institution?: string;
  onClose: () => void;
}

function SelectionEditor({
  selection,
  index,
  readings,
  onChange,
  onRemove,
}: {
  selection: Selection;
  index: number;
  readings: Readings;
  onChange: (next: Selection) => void;
  onRemove: () => void;
}) {
  const read = selection.kind === "" ? undefined : readings[selection.kind];
  const kind = selection.kind;
  return (
    <fieldset className="teach-selection ledger-stack">
      <legend>
        Selection {index + 1} (page {selection.page})
      </legend>
      <p className="ledger-muted">
        {selection.text === "" ? "No words in this box." : `“${selection.text.slice(0, 160)}”`}
      </p>
      <Field label="What is this?">
        <Select
          placeholder="Choose what it is"
          options={SELECTION_CHOICES}
          value={kind}
          onChange={(event) => {
            const value = event.target.value;
            const found = SELECTION_CHOICES.find((c) => c.value === value);
            onChange({ ...selection, kind: found === undefined ? "" : found.value });
          }}
        />
      </Field>
      {read === undefined ? null : (
        <p className="ledger-muted">PerFin read: {read ?? "nothing yet"}</p>
      )}
      {kind === "other" ? (
        <Field
          label="Describe it"
          hint="Kept as a note for your household; PerFin does not act on it."
        >
          <Input
            value={selection.note}
            maxLength={500}
            onChange={(event) => {
              onChange({ ...selection, note: event.target.value });
            }}
          />
        </Field>
      ) : null}
      {kind !== "" && kind !== "other" && kind !== "ignore" && !isTable(kind) ? (
        <Field label="Correct value (optional)" hint="Only if the page reads wrong.">
          <Input
            value={selection.value}
            maxLength={200}
            onChange={(event) => {
              onChange({ ...selection, value: event.target.value });
            }}
          />
        </Field>
      ) : null}
      {isTable(kind) ? (
        selection.columns.length < 2 ? (
          <p className="ledger-error">
            Draw the box from the table&apos;s header line so its columns can be told apart.
          </p>
        ) : (
          <div className="teach-columns" role="group" aria-label="Column roles">
            {selection.columns.map((column, at) => (
              <div key={`${String(column.x0)}-${String(at)}`} className="ledger-stack">
                <Field label={`Column ${String(at + 1)}: ${column.header || "no header"}`}>
                  <Select
                    placeholder="Choose a role"
                    options={rolesForTable(kind)}
                    value={column.role}
                    onChange={(event) => {
                      const role = rolesForTable(kind).find((r) => r.value === event.target.value);
                      const next: TeachRole | "" = role?.value ?? "";
                      const columns = selection.columns.map((c, i) =>
                        i === at ? { ...c, role: next } : c,
                      );
                      onChange({ ...selection, columns });
                    }}
                  />
                </Field>
                {column.role === "other" ? (
                  <Field label={`Describe column ${String(at + 1)}`}>
                    <Input
                      value={column.note}
                      maxLength={500}
                      onChange={(event) => {
                        const columns = selection.columns.map((c, i) =>
                          i === at ? { ...c, note: event.target.value } : c,
                        );
                        onChange({ ...selection, columns });
                      }}
                    />
                  </Field>
                ) : null}
              </div>
            ))}
          </div>
        )
      ) : null}
      {kind === "transactions" && selection.columns.length >= 2 ? (
        <>
          <Field
            label="Direction"
            hint="What the amounts of this table mean. Separate money-in and money-out columns say it themselves."
          >
            <Select
              options={DIRECTION_CHOICES}
              value={selection.direction}
              onChange={(event) => {
                const found = DIRECTION_CHOICES.find((c) => c.value === event.target.value);
                onChange({ ...selection, direction: found?.value ?? "as_printed" });
              }}
            />
          </Field>
          <Checkbox
            label="The table continues on the following pages without its header"
            checked={selection.continues}
            onChange={(event) => {
              onChange({ ...selection, continues: event.target.checked });
            }}
          />
        </>
      ) : null}
      <div className="ledger-actions">
        <Button size="sm" variant="ghost" onClick={onRemove}>
          Remove selection {index + 1}
        </Button>
      </div>
    </fieldset>
  );
}

function Outcome({ out }: { out: TeachOut }) {
  // Several tables of one kind are told apart by a number; the only one needs none.
  const named = out.tables.map((table, at) => {
    const name = table.kind === "transactions" ? "Transaction table" : "Holdings table";
    const same = out.tables.filter((other) => other.kind === table.kind).length;
    const nth = out.tables.slice(0, at).filter((other) => other.kind === table.kind).length + 1;
    return { name: same > 1 ? `${name} ${String(nth)}` : name, rows: table.rows };
  });
  return (
    <div role="status" className="ledger-stack">
      <p>
        {out.fingerprint_id === null || out.fingerprint_id === undefined
          ? "Applied to this file."
          : `Applied and remembered. ${String(out.reprocessed?.length ?? 0)} file(s) are being read again.`}
        {out.rerun === "import" ? " Its rows are being imported." : ""}
      </p>
      {out.merged_from === null || out.merged_from === undefined ? null : (
        <p>
          Added to the layout remembered before.
          {(out.kept ?? []).length > 0
            ? ` Still remembered from earlier teaching: ${keptText(out.kept ?? [])}.`
            : ""}
        </p>
      )}
      {named.map((table) => (
        <p key={table.name}>{`${table.name}: ${String(table.rows)} row(s) read.`}</p>
      ))}
      {(out.warnings ?? []).length > 0 ? (
        <ul>
          {(out.warnings ?? []).map((code) => (
            <li key={code}>{warningText(code)}</li>
          ))}
        </ul>
      ) : null}
      <p>
        <Link to="/documents/learned">See what PerFin has learned</Link>
      </p>
    </div>
  );
}

/** A preview and the reading it was made from (``readingKey``). */
interface Shown {
  key: string;
  preview: TeachPreview;
  warnings: readonly string[];
}

/** What the labels would read: the summary, the first rows and what else would be read again. */
function PreviewBlock({
  shown,
  tableNames,
  rereads,
}: {
  shown: Shown;
  /** The name of each table of the request, in order (a row says which one it is from). */
  tableNames: readonly string[];
  /** Other waiting files of the layout are read again when it is applied. */
  rereads: boolean;
}) {
  const { preview, warnings } = shown;
  return (
    <section aria-label="Preview" className="ledger-stack">
      {/* Only the summary is announced: the rows below would be read out cell by cell. */}
      <p role="status">{previewSummary(preview)}</p>
      {preview.rows.length === 0 ? null : (
        <div className="mapping-grid" role="region" aria-label="First rows read" tabIndex={0}>
          <table className="table table--compact">
            <caption className="visually-hidden">First rows read</caption>
            <thead>
              <tr>
                <th scope="col">Date</th>
                <th scope="col">Payee</th>
                <th scope="col">Amount</th>
                <th scope="col">Table</th>
              </tr>
            </thead>
            <tbody>
              {preview.rows.map((row, at) => (
                <tr key={`${String(at)}-${row.posted_date}-${row.amount}`}>
                  <td>{row.posted_date}</td>
                  <td>{row.payee ?? "Not shown"}</td>
                  <td className="align-end">{row.amount}</td>
                  <td>{tableNames.at(row.table) ?? `Table ${String(row.table + 1)}`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {preview.rows.length > 0 && preview.rows.length < preview.row_count ? (
        <p className="ledger-muted">
          The first {preview.rows.length} of {preview.row_count} rows are shown.
        </p>
      ) : null}
      {warnings.length > 0 ? (
        <ul>
          {warnings.map((code) => (
            <li key={code}>{warningText(code)}</li>
          ))}
        </ul>
      ) : null}
      {rereads && preview.similar_waiting > 0 ? (
        <p>
          {`${String(preview.similar_waiting)} other waiting file(s) with this layout will be read again.`}
        </p>
      ) : null}
      <p className="ledger-muted">Nothing is saved until you apply.</p>
    </section>
  );
}

export function TeachDialog({
  documentId,
  item = null,
  readings = {},
  institution = "",
  onClose,
}: TeachDialogProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [selections, setSelections] = useState<Selection[]>([]);
  const [institutionName, setInstitutionName] = useState(institution);
  const [remember, setRemember] = useState(false);
  const [similar, setSimilar] = useState(true);
  const [replace, setReplace] = useState(false);
  /** Another layout of the institution was taught before (409 layout-conflict). */
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The account picked here (undefined: not touched, the document's own). */
  const [pickedAccount, setPickedAccount] = useState<string | null | undefined>(undefined);
  const [shown, setShown] = useState<Shown | null>(null);
  /** The request the last successful apply sent (null: nothing applied yet). */
  const [applied, setApplied] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: docKeys.one(documentId),
    queryFn: () => getDocumentDetail(documentId),
  });
  const accounts = useAccounts();
  const documentAccount = detail.data?.account_id ?? null;
  const accountId = pickedAccount === undefined ? documentAccount : pickedAccount;
  const accountOptions = (accounts.data ?? [])
    .filter((a) => !a.is_closed || a.id === accountId)
    .map((a) => ({ value: a.id, label: accountLabel(a) }));
  if (accountId !== null && !accountOptions.some((o) => o.value === accountId)) {
    accountOptions.push({ value: accountId, label: "The account this file is filed under" });
  }
  const missing = problems(selections);
  const named = selections.find((s) => s.kind === "institution");
  const institutionSent = institutionName.trim() === "" ? (named?.text ?? "") : institutionName;
  // A preview stands only for the reading it was made from: the selections,
  // the account, and the institution its count of waiting files was taken for.
  const reading = `${readingKey(selections, accountId)}|${institutionSent.trim()}`;
  const previewed = shown !== null && shown.key === reading ? shown : null;
  const body = (preview: boolean): TeachRequest =>
    teachBody(selections, {
      institutionName: institutionSent,
      remember,
      reprocessSimilar: similar,
      itemId: item?.id ?? null,
      replace,
      accountId,
      preview,
    });
  const onError = (caught: unknown) => {
    setConflict(isApiError(caught) && caught.slug === "layout-conflict");
    setError(errorMessage(caught));
  };
  const look = useMutation({
    mutationFn: (asked: { key: string; body: TeachRequest }) =>
      teachDocument(documentId, asked.body),
    onSuccess: (out, asked) => {
      if (out.preview === null || out.preview === undefined) {
        setShown(null);
        setError("PerFin could not show what it would read from these selections.");
        return;
      }
      setError(null);
      setShown({ key: asked.key, preview: out.preview, warnings: out.warnings ?? [] });
    },
    onError,
  });
  const teach = useMutation({
    mutationFn: (sent: TeachRequest) => teachDocument(documentId, sent),
    onSuccess: (_out, sent) => {
      setError(null);
      setApplied(JSON.stringify(sent));
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
      toast.notify({ title: "PerFin learned from this file", tone: "success" });
    },
    onError,
  });
  // What was just applied is not offered again until something asked for changes.
  const done = applied !== null && applied === JSON.stringify(body(false));
  const incomplete = missing.length > 0 || (selections.length === 0 && !remember);
  const tableNames = selections.flatMap((s, index) =>
    isTable(s.kind) ? [`Selection ${String(index + 1)}`] : [],
  );
  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Teach PerFin"
      description="Click a word or drag a box on the page, then say what it is."
    >
      <div className="teach-dialog">
        <PageViewer
          documentId={documentId}
          regions={item?.regions ?? []}
          selections={selections.map((s) => ({
            page: s.page,
            bbox: s.bbox,
            label: selectionLabel(s.kind),
          }))}
          onSelect={(page, bbox, words) => {
            setSelections((previous) => [...previous, newSelection(page, bbox, words)]);
          }}
        />
        <div className="ledger-stack">
          <section aria-label="What PerFin read">
            <h3>What PerFin read</h3>
            <dl>
              {SHOWN.map((kind) => (
                <div key={kind}>
                  <dt>{selectionLabel(kind)}</dt>
                  {/* eslint-disable-next-line security/detect-object-injection -- keyed by the closed SelectionKind union */}
                  <dd>{readings[kind] ?? "Not found"}</dd>
                </div>
              ))}
            </dl>
          </section>
          {selections.length === 0 ? (
            <p className="ledger-muted">Nothing selected yet.</p>
          ) : null}
          {selections.map((selection, index) => (
            <SelectionEditor
              key={selection.id}
              selection={selection}
              index={index}
              readings={readings}
              onChange={(next) => {
                setSelections((previous) => previous.map((s) => (s.id === next.id ? next : s)));
              }}
              onRemove={() => {
                setSelections((previous) => previous.filter((s) => s.id !== selection.id));
              }}
            />
          ))}
          <Field
            label="Account for this file"
            hint="The account its rows belong to. Leave it on “Not chosen” to keep what PerFin worked out."
          >
            <Select
              placeholder="Not chosen"
              options={accountOptions}
              value={accountId ?? ""}
              onChange={(event) => {
                setPickedAccount(event.target.value === "" ? null : event.target.value);
              }}
            />
          </Field>
          <Field label="Institution" hint="Needed to remember the layout.">
            <Input
              value={institutionName}
              placeholder={named?.text ?? ""}
              maxLength={200}
              onChange={(event) => {
                setInstitutionName(event.target.value);
              }}
            />
          </Field>
          <Checkbox
            label="Remember this layout for the next files"
            checked={remember}
            onChange={(event) => {
              setRemember(event.target.checked);
            }}
          />
          {remember ? (
            <Checkbox
              label="Read files already received with this layout again"
              checked={similar}
              onChange={(event) => {
                setSimilar(event.target.checked);
              }}
            />
          ) : null}
          {remember ? (
            <Checkbox
              label="Replace the layout remembered for this institution (what was taught for it before is no longer used)"
              hint={
                conflict
                  ? "Another layout of this institution was taught before: tick this to replace it."
                  : "Leave it unticked to add to what was taught before; tick it to drop earlier lines, notes or labels taught by mistake."
              }
              checked={replace}
              onChange={(event) => {
                setReplace(event.target.checked);
              }}
            />
          ) : null}
          {missing.length > 0 && selections.length > 0 ? (
            <ul className="ledger-muted" aria-label="Still to do">
              {missing.map((text) => (
                <li key={text}>{text}</li>
              ))}
            </ul>
          ) : null}
          {error === null ? null : (
            <p role="alert" className="ledger-error">
              {error}
            </p>
          )}
          {previewed === null || done ? null : (
            <PreviewBlock shown={previewed} tableNames={tableNames} rereads={remember && similar} />
          )}
          {previewed === null && !incomplete ? (
            <p className="ledger-muted">
              Preview what PerFin would read before applying. Nothing is saved by a preview.
            </p>
          ) : null}
          {teach.data === undefined ? null : <Outcome out={teach.data} />}
          <div className="ledger-actions">
            <Button
              disabled={incomplete}
              loading={look.isPending}
              onClick={() => {
                look.mutate({ key: reading, body: body(true) });
              }}
            >
              Preview
            </Button>
            <Button
              variant="primary"
              disabled={
                incomplete ||
                previewed === null ||
                done ||
                (remember && institutionName.trim() === "" && named === undefined)
              }
              loading={teach.isPending}
              onClick={() => {
                teach.mutate(body(false));
              }}
            >
              {remember ? "Apply and remember" : "Apply to this file"}
            </Button>
            <Button variant="ghost" onClick={onClose}>
              {teach.data === undefined ? "Cancel" : "Done"}
            </Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
