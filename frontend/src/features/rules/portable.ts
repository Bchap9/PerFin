// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The rules export file (`GET /rules/export`, also the body of `POST
 * /rules/import`): reading one a person chose, checking that it parses and
 * looks like an export before anything is sent, and summarising it.
 *
 * The check is deliberately shallow. It answers "is this a rules export?";
 * whether each condition and action is valid in this household is the
 * server's decision, and its refusal is shown as it is.
 */
import { errorMessage, isApiError } from "../../lib/api/client";
import type { RuleExport, RuleImport, RuleImportMode } from "./api";

export const RULES_FORMAT = "perfin-rules";
export const RULES_VERSION = 1;
/** Most rules a household can have (backend `RULES_MAX`). */
export const RULES_MAX = 500;
/** A full export of 500 rules is well under this; anything larger is some other file. */
export const RULES_FILE_MAX_BYTES = 5 * 1024 * 1024;
/** How an account the exporting person could not see appears in an export. */
const HIDDEN_ACCOUNT = "00000000-0000-0000-0000-000000000000";

const REFERENCE_KINDS = ["categories", "payees", "tags", "accounts"] as const;
type ReferenceKind = (typeof REFERENCE_KINDS)[number];

export interface RulesFile {
  /** The document as it will be sent (known members only). */
  document: RuleExport;
  names: string[];
  enabled: number;
  /** How many names of each kind the file carries for matching elsewhere. */
  references: Record<ReferenceKind, number>;
  /** A rule names an account that was hidden from whoever exported the file. */
  hiddenAccount: boolean;
}

export type ParsedRulesFile = { ok: true; file: RulesFile } | { ok: false; message: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function refuse(message: string): ParsedRulesFile {
  return { ok: false, message };
}

function isReferenceList(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.every((item) => isRecord(item) && typeof item["id"] === "string" && typeof item["name"] === "string")
  );
}

function isRuleShaped(value: unknown): value is Record<string, unknown> & { name: string } {
  return (
    isRecord(value) &&
    typeof value["name"] === "string" &&
    value["name"].trim() !== "" &&
    isRecord(value["conditions"]) &&
    Array.isArray(value["actions"])
  );
}

/** Check the text of a chosen file; a refusal says what is wrong in plain words. */
export function parseRulesFile(text: string): ParsedRulesFile {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text) as unknown;
  } catch {
    return refuse("This file is not valid JSON, so it cannot be a rules export.");
  }
  if (!isRecord(parsed) || parsed["format"] !== RULES_FORMAT) {
    return refuse('This file does not look like a PerFin rules export: it is missing the "perfin-rules" format marker.');
  }
  if (parsed["version"] !== RULES_VERSION) {
    return refuse(`This rules export is version ${String(parsed["version"])}. This PerFin reads version ${String(RULES_VERSION)}.`);
  }
  const listed = parsed["rules"];
  if (!Array.isArray(listed)) {
    return refuse("This file has no list of rules.");
  }
  const rules: unknown[] = listed;
  if (rules.length === 0) {
    return refuse("This file holds no rules, so there is nothing to import.");
  }
  if (rules.length > RULES_MAX) {
    return refuse(`This file holds ${String(rules.length)} rules. A household can have at most ${String(RULES_MAX)}.`);
  }
  const names: string[] = [];
  let enabled = 0;
  for (const [index, rule] of rules.entries()) {
    if (!isRuleShaped(rule)) {
      return refuse(`Rule ${String(index + 1)} in this file is incomplete: a rule needs a name, conditions and actions.`);
    }
    names.push(rule.name);
    // `enabled` defaults to true on the server when it is left out.
    enabled += rule["enabled"] === false ? 0 : 1;
  }
  const references = parsed["references"];
  const counts: Record<ReferenceKind, number> = { categories: 0, payees: 0, tags: 0, accounts: 0 };
  if (references !== undefined) {
    if (!isRecord(references)) {
      return refuse("The list of names the rules refer to is damaged.");
    }
    for (const kind of REFERENCE_KINDS) {
      // eslint-disable-next-line security/detect-object-injection -- kind is one of the four fixed names
      const list = references[kind];
      if (list === undefined) {
        continue;
      }
      if (!isReferenceList(list)) {
        return refuse("The list of names the rules refer to is damaged.");
      }
      // eslint-disable-next-line security/detect-object-injection -- kind is one of the four fixed names
      counts[kind] = (list as unknown[]).length;
    }
  }
  // Shape-checked above; the server validates every condition and action.
  const document = {
    format: RULES_FORMAT,
    version: RULES_VERSION,
    rules,
    ...(references === undefined ? {} : { references }),
  } as RuleExport;
  return {
    ok: true,
    file: { document, names, enabled, references: counts, hiddenAccount: JSON.stringify(rules).includes(HIDDEN_ACCOUNT) },
  };
}

export function importBody(file: RulesFile, mode: RuleImportMode): RuleImport {
  return { ...file.document, mode };
}

export function rulesFilename(now: Date = new Date()): string {
  return `perfin-rules-${now.toISOString().slice(0, 10)}.json`;
}

/** The file's text. FileReader rather than `Blob.text()`, which older engines lack. */
export function readFileText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      resolve(typeof reader.result === "string" ? reader.result : "");
    };
    reader.onerror = () => {
      reject(new Error("The file could not be read."));
    };
    reader.readAsText(file);
  });
}

export function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

/** "3 categories, 1 payee and 2 tags", or `null` when the file names nothing. */
export function referenceSummary(references: RulesFile["references"]): string | null {
  const parts = [
    references.categories > 0 ? plural(references.categories, "category", "categories") : null,
    references.payees > 0 ? plural(references.payees, "payee", "payees") : null,
    references.tags > 0 ? plural(references.tags, "tag", "tags") : null,
    references.accounts > 0 ? plural(references.accounts, "account", "accounts") : null,
  ].filter((part): part is string => part !== null);
  if (parts.length === 0) {
    return null;
  }
  const last = parts.pop() ?? "";
  return parts.length === 0 ? last : `${parts.join(", ")} and ${last}`;
}

function sentence(message: string): string {
  return message.endsWith(".") ? message : `${message}.`;
}

/**
 * The server's reason for refusing an import. A rule that does not validate
 * arrives as a field path ("rules.3.actions.0..."); name the rule instead.
 */
export function importProblem(error: unknown, names: readonly string[]): string {
  if (isApiError(error) && error.slug === "validation-failed") {
    for (const issue of error.issues) {
      const [, list, index] = issue.loc;
      if (list === "rules" && typeof index === "number") {
        const name = names.at(index);
        // What is left of the path says where in the rule: "actions, set_category, category_id".
        const where = issue.loc.slice(3).filter((part) => typeof part === "string").join(", ");
        return `Rule ${String(index + 1)}${name === undefined ? "" : ` ("${name}")`} is not valid here${where === "" ? "" : ` (${where})`}: ${sentence(issue.msg)}`;
      }
    }
    const first = error.issues[0];
    if (first !== undefined) {
      return `The server does not accept this file: ${sentence(first.msg)}`;
    }
  }
  return errorMessage(error);
}
