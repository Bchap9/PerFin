// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Rules as a file: "Export rules" saves `GET /rules/export` as JSON, and
 * "Import rules" reads such a file, shows what it holds and what importing
 * will do, then sends it to `POST /rules/import` (owner or admin).
 *
 * What the server does with an import (backend `rules.service.import_rules`):
 * the file's rules are added after the existing ones, or replace them all;
 * nothing is compared with the rules already here. A category, payee, tag or
 * account a rule refers to is kept when its id exists in this household and
 * is otherwise looked up by the name the file carries for it (categories by
 * their full path). Nothing is created, and one missing or ambiguous name
 * refuses the whole import.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { queryRoots } from "../../lib/queryKeys";
import { ErrorLine } from "../planning/PlanningPage";
import { useIdempotencyKey } from "../../lib/idempotency";
import { saveBlob } from "../settings/saveFile";
import { exportRules, importRules, type Rule, type RuleImportMode } from "./api";
import {
  importBody,
  importProblem,
  parseRulesFile,
  plural,
  readFileText,
  referenceSummary,
  RULES_FILE_MAX_BYTES,
  RULES_MAX,
  rulesFilename,
  type RulesFile,
} from "./portable";

const NAMES_SHOWN = 8;

export function ExportRulesButton({ rules }: { rules: readonly Rule[] }) {
  const { notify } = useToast();
  const [busy, setBusy] = useState(false);
  const hidden = rules.filter((rule) => rule.redacted).length;
  const save = async (): Promise<void> => {
    setBusy(true);
    try {
      const document = await exportRules();
      saveBlob(new Blob([`${JSON.stringify(document, null, 2)}\n`], { type: "application/json" }), rulesFilename());
      notify({
        tone: hidden > 0 ? "warning" : "success",
        title: `${plural(document.rules.length, "rule", "rules")} exported`,
        ...(hidden > 0
          ? {
              description: `${plural(hidden, "rule refers", "rules refer")} to accounts you cannot see. The file leaves those accounts out, so importing it is refused. Ask an owner or admin to export the rules instead.`,
              durationMs: null,
            }
          : {}),
      });
    } catch (caught) {
      notify({ tone: "error", title: "Rules not exported", description: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button disabled={rules.length === 0} loading={busy} onClick={() => void save()}>
      Export rules
    </Button>
  );
}

type Chosen =
  | { state: "none" }
  | { state: "reading"; filename: string }
  | { state: "refused"; filename: string; message: string }
  | { state: "ready"; filename: string; pick: number; file: RulesFile };

function FileSummary({ filename, file }: { filename: string; file: RulesFile }) {
  const disabled = file.names.length - file.enabled;
  const references = referenceSummary(file.references);
  const more = file.names.length - NAMES_SHOWN;
  return (
    <div className="planning-form" role="status" aria-live="polite">
      <p>
        <strong>{filename}</strong> holds {plural(file.names.length, "rule", "rules")}
        {disabled === 0 ? "" : ` (${String(disabled)} switched off; they stay off)`}:
      </p>
      <ul className="planning-list" aria-label="Rules in the file">
        {file.names.slice(0, NAMES_SHOWN).map((name, index) => (
          <li key={`${String(index)}:${name}`}>{name}</li>
        ))}
        {more > 0 ? <li className="muted">and {String(more)} more</li> : null}
      </ul>
      {references === null ? null : (
        <p className="muted">The file names {references} for the rules to be matched with yours.</p>
      )}
      {file.hiddenAccount ? (
        <p role="alert" className="auth-error">
          At least one rule refers to an account that was hidden from the person who exported this file. The file does not say
          which account that is, so the import will be refused until that condition is removed from the file.
        </p>
      ) : null}
    </div>
  );
}

export function ImportRulesDialog({ existing, onClose }: { existing: number; onClose: () => void }) {
  const modeName = useId();
  const keys = useIdempotencyKey();
  const [chosen, setChosen] = useState<Chosen>({ state: "none" });
  const [mode, setMode] = useState<RuleImportMode>("append");
  const [picks, setPicks] = useState(0);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ imported: number; total: number; removed: number } | null>(null);
  const queryClient = useQueryClient();

  const choose = async (picked: File | null): Promise<void> => {
    setProblem(null);
    if (picked === null) {
      setChosen({ state: "none" });
      return;
    }
    const filename = picked.name;
    if (picked.size > RULES_FILE_MAX_BYTES) {
      setChosen({ state: "refused", filename, message: "This file is too large to be a rules export." });
      return;
    }
    setChosen({ state: "reading", filename });
    try {
      const parsed = parseRulesFile(await readFileText(picked));
      const pick = picks + 1;
      setPicks(pick);
      setChosen(parsed.ok ? { state: "ready", filename, pick, file: parsed.file } : { state: "refused", filename, message: parsed.message });
    } catch (caught) {
      setChosen({ state: "refused", filename, message: caught instanceof Error ? caught.message : errorMessage(caught) });
    }
  };

  const ready = chosen.state === "ready" ? chosen : null;
  const count = ready?.file.names.length ?? 0;
  const after = (mode === "replace" ? 0 : existing) + count;
  const tooMany = ready !== null && after > RULES_MAX;

  const submit = async (): Promise<void> => {
    if (ready === null || tooMany) {
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      // One key per file and choice: a second click or a retry after a lost answer cannot import twice.
      const rules = await importRules(importBody(ready.file, mode), keys.keyFor(`${String(ready.pick)}:${mode}`));
      keys.done();
      setResult({ imported: count, total: rules.items.length, removed: mode === "replace" ? existing : 0 });
      await queryClient.invalidateQueries({ queryKey: queryRoots.rules });
    } catch (caught) {
      setProblem(importProblem(caught, ready.file.names));
    } finally {
      setBusy(false);
    }
  };

  if (result !== null) {
    return (
      <Dialog
        open
        onClose={onClose}
        title="Rules imported"
        footer={
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        }
      >
        <div className="planning-form" role="status">
          <p>
            Imported {plural(result.imported, "rule", "rules")}
            {result.removed > 0 ? `, replacing the ${plural(result.removed, "rule", "rules")} you had` : ""}. You now have{" "}
            {plural(result.total, "rule", "rules")}.
          </p>
          <p className="muted">
            The imported rules run on new transactions from now on. To run them over transactions already in the ledger, use
            Apply to existing transactions.
          </p>
        </div>
      </Dialog>
    );
  }

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Import rules"
      description="Choose a rules file made with Export rules, here or in another PerFin household."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={mode === "replace" ? "danger" : "primary"}
            loading={busy}
            disabled={ready === null || tooMany}
            onClick={() => void submit()}
          >
            {ready === null
              ? "Import rules"
              : mode === "replace"
                ? `Replace my rules with ${plural(count, "rule", "rules")}`
                : `Add ${plural(count, "rule", "rules")}`}
          </Button>
        </>
      }
    >
      <div className="planning-form">
        <Field label="Rules file" hint="A .json file. It is checked on this device before anything is sent.">
          <Input
            type="file"
            accept=".json,application/json"
            onChange={(event) => {
              void choose(event.target.files?.item(0) ?? null);
            }}
          />
        </Field>
        {chosen.state === "reading" ? <Spinner label="Reading the file" /> : null}
        {chosen.state === "refused" ? <ErrorLine message={chosen.message} /> : null}
        {ready === null ? null : (
          <>
            <FileSummary filename={ready.filename} file={ready.file} />
            <fieldset className="planning-form">
              <legend>What to do with the rules you already have ({String(existing)})</legend>
              <label>
                <input
                  type="radio"
                  name={modeName}
                  value="append"
                  checked={mode === "append"}
                  onChange={() => {
                    setMode("append");
                  }}
                />{" "}
                Keep them and add the file's rules after them
                <span className="muted">
                  {" "}
                  (the new rules run last; nothing is compared, so a rule you already have is added a second time)
                </span>
              </label>
              <label>
                <input
                  type="radio"
                  name={modeName}
                  value="replace"
                  checked={mode === "replace"}
                  onChange={() => {
                    setMode("replace");
                  }}
                />{" "}
                Delete them and use only the file's rules
                <span className="muted">
                  {" "}
                  ({existing === 0 ? "you have none" : `${plural(existing, "rule is", "rules are")} deleted for good`}; transactions
                  they already changed keep their changes)
                </span>
              </label>
            </fieldset>
            <p className="muted">
              Categories, payees, tags and accounts are matched for you: one the file shares with this household is used as it is,
              any other is found by its name (a category by its full path, such as Food / Groceries). Nothing is created. If a
              name is missing here, or fits more than one, nothing is imported and you are told which names to create first.
            </p>
            {tooMany ? (
              <ErrorLine
                message={`That would make ${String(after)} rules. A household can have at most ${String(RULES_MAX)}.`}
              />
            ) : null}
          </>
        )}
        <ErrorLine message={problem} />
      </div>
    </Dialog>
  );
}
