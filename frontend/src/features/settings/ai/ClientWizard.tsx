// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Create-client wizard: 1 basics (name, transport, expiry, mode), 2 options
 * with the live exposure preview, 3 review and create. Creating needs a
 * fresh second factor (step-up); the token comes back once.
 */
import { useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Checkbox } from "../../../components/Checkbox";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { Select } from "../../../components/Select";
import { isApiError } from "../../../lib/api/client";
import { authErrorMessage, fieldErrorsOf } from "../../auth/errors";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";
import { createClient, type AiClientCreate, type AiClientCreated, type AiMode, type AiOptions, type AiTransport } from "./api";
import { LivePreview } from "./ExposurePreview";
import { parseTerms, TextArea } from "./TextArea";

export const MAX_EXPIRY_DAYS = 365;
const MAX_NAME = 100;
const MAX_TERM = 100;
const MAX_TERMS = 200;
const MAX_CAP = 3660;

export interface Draft {
  name: string;
  mode: AiMode;
  transport: AiTransport;
  days: string;
  rounding: "" | "1" | "10" | "100";
  hideNicknames: boolean;
  hideLocation: boolean;
  cap: string;
  denylist: string;
  aliases: string;
}

export const EMPTY_DRAFT: Draft = {
  name: "",
  mode: "detail",
  transport: "stdio",
  days: "30",
  rounding: "",
  hideNicknames: false,
  hideLocation: false,
  cap: "",
  denylist: "",
  aliases: "",
};

function termsError(label: string, text: string): string | undefined {
  const terms = parseTerms(text);
  if (terms.length > MAX_TERMS) {
    return `At most ${String(MAX_TERMS)} ${label}.`;
  }
  if (terms.some((term) => term.length > MAX_TERM)) {
    return `Each entry has at most ${String(MAX_TERM)} characters.`;
  }
  return undefined;
}

/** Field errors of the options (step 2) and of the basics (step 1). */
export function validate(draft: Draft): { basics: Record<string, string>; options: Record<string, string> } {
  const basics: Record<string, string> = {};
  const options: Record<string, string> = {};
  const name = draft.name.trim();
  if (name === "" || name.length > MAX_NAME) {
    basics["name"] = `Give the client a name of 1 to ${String(MAX_NAME)} characters.`;
  }
  const days = Number(draft.days);
  if (!Number.isInteger(days) || days < 1 || days > MAX_EXPIRY_DAYS) {
    basics["expires_in_days"] = `Choose 1 to ${String(MAX_EXPIRY_DAYS)} days.`;
  }
  if (draft.cap.trim() !== "") {
    const cap = Number(draft.cap);
    if (!Number.isInteger(cap) || cap < 1 || cap > MAX_CAP) {
      options["date_range_cap"] = `Choose 1 to ${String(MAX_CAP)} days, or leave empty for no limit.`;
    }
  }
  const denylist = termsError("entries", draft.denylist);
  if (denylist !== undefined) {
    options["custom_denylist"] = denylist;
  }
  const aliases = termsError("aliases", draft.aliases);
  if (aliases !== undefined) {
    options["member_aliases"] = aliases;
  }
  return { basics, options };
}

/** The exposure options of a draft (for the preview), or `null` while the options have errors. */
export function optionsOf(draft: Draft): AiOptions | null {
  const { options } = validate(draft);
  if (Object.keys(options).length > 0) {
    return null;
  }
  return {
    name: "Preview",
    mode: draft.mode,
    transport: draft.transport,
    rounding: draft.rounding === "" ? null : (Number(draft.rounding) as 1 | 10 | 100),
    hide_nicknames: draft.hideNicknames,
    hide_merchant_location: draft.hideLocation,
    date_range_cap: draft.cap.trim() === "" ? null : Number(draft.cap),
    custom_denylist: parseTerms(draft.denylist),
    member_aliases: parseTerms(draft.aliases),
  };
}

function createError(error: unknown): string {
  if (isApiError(error) && error.slug === "too-many-ai-clients") {
    return "You have the maximum number of active AI clients. Revoke one first.";
  }
  if (isApiError(error) && error.slug === "ai-access-disabled") {
    return "AI access is switched off, so no new client can be created.";
  }
  return authErrorMessage(error);
}

const STEPS = ["Basics", "Options and preview", "Review"] as const;

export function ClientWizard({
  onCreated,
  onCancel,
}: {
  onCreated: (client: AiClientCreated) => void;
  onCancel: () => void;
}) {
  const runWithStepUp = useStepUp();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]): void => {
    setDraft((previous) => ({ ...previous, [key]: value }));
  };
  const { basics, options } = validate(draft);
  const [shown, setShown] = useState(false);
  const basicsErrors = shown ? basics : {};
  const optionsErrors = options;

  const next = (): void => {
    if (step === 0) {
      setShown(true);
      if (Object.keys(basics).length > 0) {
        return;
      }
    }
    if (step === 1 && Object.keys(options).length > 0) {
      return;
    }
    setStep((current) => Math.min(current + 1, STEPS.length - 1));
  };

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (step < STEPS.length - 1) {
      next();
      return;
    }
    const preview = optionsOf(draft);
    if (preview === null || Object.keys(basics).length > 0) {
      setError("Fix the errors in the earlier steps first.");
      return;
    }
    const body: AiClientCreate = {
      ...preview,
      name: draft.name.trim(),
      expires_in_days: Number(draft.days),
    };
    setBusy(true);
    setError(null);
    setServerErrors({});
    try {
      const created = await runWithStepUp(() => createClient(body));
      onCreated(created);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(createError(caught));
        setServerErrors(fieldErrorsOf(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  const summary: [string, string][] = [
    ["Name", draft.name.trim()],
    ["Mode", draft.mode === "detail" ? "Detail" : "Aggregates"],
    ["Transport", draft.transport],
    ["Expires after", `${draft.days} days`],
    ["Rounding", draft.rounding === "" ? "Exact amounts" : `Nearest ${draft.rounding}`],
    ["Account nicknames", draft.hideNicknames ? "Hidden" : "Shown"],
    ["Merchant location", draft.hideLocation ? "Hidden" : "Shown"],
    ["Date range limit", draft.cap.trim() === "" ? "None" : `${draft.cap} days`],
    ["Custom denylist", `${String(parseTerms(draft.denylist).length)} entries`],
    ["Member aliases", `${String(parseTerms(draft.aliases).length)} entries`],
  ];

  return (
    <Card title="New AI client" headingLevel={3}>
      <form
        className="auth-stack"
        noValidate
        aria-label="New AI client"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <ol className="ai-steps" aria-label="Steps">
          {STEPS.map((label, index) => (
            <li key={label} aria-current={index === step ? "step" : undefined} className={index === step ? "ai-steps__on" : undefined}>
              {String(index + 1)}. {label}
            </li>
          ))}
        </ol>

        {step === 0 ? (
          <>
            <Field label="Client name" hint="Where it runs, for example 'Claude Desktop, laptop'." error={basicsErrors["name"] ?? serverErrors["name"]} required>
              <Input
                name="client-name"
                maxLength={MAX_NAME}
                value={draft.name}
                onChange={(event) => {
                  set("name", event.target.value);
                }}
              />
            </Field>
            <Field
              label="Mode"
              hint="Aggregates shares totals and trends only: no transactions, payees or nicknames. A good first step."
            >
              <Select
                name="client-mode"
                value={draft.mode}
                options={[
                  { value: "detail", label: "Detail (transaction-level financial facts)" },
                  { value: "aggregates", label: "Aggregates (totals and trends only)" },
                ]}
                onChange={(event) => {
                  set("mode", event.target.value as AiMode);
                }}
              />
            </Field>
            <Field label="Transport" hint="stdio: the assistant starts the server itself. http: a server behind your proxy.">
              <Select
                name="client-transport"
                value={draft.transport}
                options={[
                  { value: "stdio", label: "stdio" },
                  { value: "http", label: "http" },
                ]}
                onChange={(event) => {
                  set("transport", event.target.value as AiTransport);
                }}
              />
            </Field>
            <Field
              label="Expires after (days)"
              hint={`The token stops working then. At most ${String(MAX_EXPIRY_DAYS)} days.`}
              error={basicsErrors["expires_in_days"] ?? serverErrors["expires_in_days"]}
              required
            >
              <Input
                name="client-days"
                type="number"
                inputMode="numeric"
                min={1}
                max={MAX_EXPIRY_DAYS}
                value={draft.days}
                onChange={(event) => {
                  set("days", event.target.value);
                }}
              />
            </Field>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <fieldset className="ai-options">
              <legend>What is shared</legend>
              <Field label="Round amounts" hint="Rounds every amount to the nearest unit.">
                <Select
                  name="client-rounding"
                  value={draft.rounding}
                  options={[
                    { value: "", label: "Exact amounts" },
                    { value: "1", label: "Nearest 1" },
                    { value: "10", label: "Nearest 10" },
                    { value: "100", label: "Nearest 100" },
                  ]}
                  onChange={(event) => {
                    set("rounding", event.target.value as Draft["rounding"]);
                  }}
                />
              </Field>
              <Checkbox
                label="Hide account nicknames"
                hint="Accounts appear as 'Checking A', 'Savings B'."
                checked={draft.hideNicknames}
                onChange={(event) => {
                  set("hideNicknames", event.target.checked);
                }}
              />
              <Checkbox
                label="Hide merchant location"
                hint="Removes the trailing city and state of a merchant."
                checked={draft.hideLocation}
                onChange={(event) => {
                  set("hideLocation", event.target.checked);
                }}
              />
              <Field
                label="Date range limit (days)"
                hint="The furthest back the client may ask. Empty: no limit."
                error={optionsErrors["date_range_cap"] ?? serverErrors["date_range_cap"]}
              >
                <Input
                  name="client-cap"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={MAX_CAP}
                  value={draft.cap}
                  onChange={(event) => {
                    set("cap", event.target.value);
                  }}
                />
              </Field>
              <Field
                label="Custom denylist"
                hint="One word or phrase per line (an ex-employer, a school). Matches become [REDACTED]."
                error={optionsErrors["custom_denylist"] ?? serverErrors["custom_denylist"]}
              >
                <TextArea
                  name="client-denylist"
                  rows={3}
                  value={draft.denylist}
                  onChange={(event) => {
                    set("denylist", event.target.value);
                  }}
                />
              </Field>
              <Field
                label="Member aliases"
                hint="One nickname per line that should be treated like a member's name. Matches become [MEMBER]."
                error={optionsErrors["member_aliases"] ?? serverErrors["member_aliases"]}
              >
                <TextArea
                  name="client-aliases"
                  rows={2}
                  value={draft.aliases}
                  onChange={(event) => {
                    set("aliases", event.target.value);
                  }}
                />
              </Field>
            </fieldset>
            <h4 className="auth-subheading">Exposure preview</h4>
            <p className="auth-muted">
              Every tool, run on your real data with these options. Highlighted parts are what the scrubber removed.
              Nothing is saved or sent.
            </p>
            <LivePreview options={optionsOf(draft)} />
          </>
        ) : null}

        {step === 2 ? (
          <>
            <dl className="ai-summary">
              {summary.map(([term, value]) => (
                <div key={term}>
                  <dt>{term}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            <p className="auth-muted">
              Creating the client asks for your second factor. The token is shown once and PerFin keeps only a hash.
            </p>
          </>
        ) : null}

        {error === null ? null : (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <div className="auth-actions">
          <Button type="button" onClick={onCancel}>
            Cancel
          </Button>
          {step > 0 ? (
            <Button
              type="button"
              onClick={() => {
                setStep(step - 1);
              }}
            >
              Back
            </Button>
          ) : null}
          {step < STEPS.length - 1 ? (
            <Button type="button" variant="primary" onClick={next}>
              Next
            </Button>
          ) : (
            <Button type="submit" variant="primary" loading={busy}>
              Create client
            </Button>
          )}
        </div>
      </form>
    </Card>
  );
}
