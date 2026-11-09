// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Teach PerFin": show PerFin where the facts of a document are. The page is
 * shown; a click picks a word, a drag picks a box (or a line is picked from
 * a list, by keyboard). Each selection is then named from a list that always
 * ends with "Other — describe it" and "Ignore this"; a table gets a role per
 * column. The labels are applied to the document and, if asked, remembered
 * for the next files of the same layout (docs/INGESTION.md).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
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
import { PageViewer } from "../documents/PageViewer";
import { payloadString, teachDocument, type ReviewItem, type TeachOut } from "./api";
import {
  SELECTION_CHOICES,
  isTable,
  keptText,
  newSelection,
  problems,
  rolesForTable,
  selectionLabel,
  teachBody,
  warningText,
  type Selection,
  type SelectionKind,
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
      <div className="ledger-actions">
        <Button size="sm" variant="ghost" onClick={onRemove}>
          Remove selection {index + 1}
        </Button>
      </div>
    </fieldset>
  );
}

function Outcome({ out }: { out: TeachOut }) {
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
      {out.tables.map((table) => (
        <p key={table.kind}>
          {table.kind === "transactions" ? "Transaction table" : "Holdings table"}: {table.rows}{" "}
          row(s) read.
        </p>
      ))}
      {(out.warnings ?? []).length > 0 ? (
        <ul>
          {(out.warnings ?? []).map((code) => (
            <li key={code}>{warningText(code)}</li>
          ))}
        </ul>
      ) : null}
    </div>
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
  const missing = problems(selections);
  const named = selections.find((s) => s.kind === "institution");
  const teach = useMutation({
    mutationFn: () =>
      teachDocument(
        documentId,
        teachBody(selections, {
          institutionName: institutionName.trim() === "" ? (named?.text ?? "") : institutionName,
          remember,
          reprocessSimilar: similar,
          itemId: item?.id ?? null,
          replace,
        }),
      ),
    onSuccess: () => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryRoots.review });
      void queryClient.invalidateQueries({ queryKey: queryRoots.documents });
      void queryClient.invalidateQueries({ queryKey: queryRoots.transactions });
      toast.notify({ title: "PerFin learned from this file", tone: "success" });
    },
    onError: (caught) => {
      setConflict(isApiError(caught) && caught.slug === "layout-conflict");
      setError(errorMessage(caught));
    },
  });
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
          {teach.data === undefined ? null : <Outcome out={teach.data} />}
          <div className="ledger-actions">
            <Button
              variant="primary"
              disabled={
                missing.length > 0 ||
                (selections.length === 0 && !remember) ||
                (remember && institutionName.trim() === "" && named === undefined)
              }
              loading={teach.isPending}
              onClick={() => {
                teach.mutate();
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
