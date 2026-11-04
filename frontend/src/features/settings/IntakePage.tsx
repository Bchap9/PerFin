// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { canManageMembers, useAuthSession } from "../auth/session";
import { isStepUpCancelled, useStepUp } from "../auth/StepUp";
import {
  getIntake,
  intakeKey,
  listMembers,
  patchIntake,
  rotateAlias,
  settingsKeys,
  testIntake,
  type MailIntake,
  type MailTest,
} from "./api";
import { buildUpdate, describeTest, formFrom, type IntakeForm } from "./intake";
import { SettingsPage, when } from "./SettingsLayout";

function AliasCard({ intake }: { intake: MailIntake }) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const rotate = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await runWithStepUp(() => rotateAlias());
      notify({ tone: "success", title: "Alias replaced" });
      await queryClient.invalidateQueries({ queryKey: intakeKey });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(errorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Mailbox alias">
      <p>
        Mail only counts when its To or Delivered-To address contains this secret token (for example
        <code> receipts+TOKEN@your-domain</code>). Replace it if it has leaked; the old one stops working at once.
      </p>
      <p>
        Token: <code data-testid="alias-token">{intake.alias_token ?? "not created yet"}</code>
      </p>
      <p className="ledger-muted">Last replaced: {when(intake.alias_rotated_at)}</p>
      {error === null ? null : (
        <p role="alert" className="ledger-error">
          {error}
        </p>
      )}
      <Button loading={busy} onClick={() => void rotate()}>
        Replace the alias
      </Button>
    </Card>
  );
}

function MailboxForm({ intake, householdId }: { intake: MailIntake; householdId: string }) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const members = useQuery({ queryKey: settingsKeys.members(householdId), queryFn: () => listMembers(householdId) });
  const [form, setForm] = useState<IntakeForm>(() => formFrom(intake));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<MailTest | null>(null);
  const test = useMutation({
    mutationFn: () => testIntake(),
    onSuccess: setTestResult,
    onError: (caught) => {
      setFailure(errorMessage(caught));
    },
  });
  const set = (patch: Partial<IntakeForm>): void => {
    setForm({ ...form, ...patch });
  };
  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const built = buildUpdate(form);
    setErrors(built.errors);
    if (built.body === null) {
      return;
    }
    const body = built.body;
    setBusy(true);
    setFailure(null);
    try {
      const saved = await runWithStepUp(() => patchIntake(body));
      setForm(formFrom(saved));
      notify({ tone: "success", title: "Mailbox settings saved" });
      await queryClient.invalidateQueries({ queryKey: intakeKey });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setFailure(errorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Receipts mailbox">
      {intake.instance_enabled ? null : (
        <p role="note" className="ledger-muted">
          Email intake is switched off for this PerFin instance by its administrator, so nothing is read even when enabled here.
        </p>
      )}
      <form
        className="ledger-stack"
        aria-label="Mailbox settings"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <Checkbox label="Read this mailbox for receipts and statements" checked={form.enabled} onChange={(e) => { set({ enabled: e.target.checked }); }} />
        <Field label="Mail server (IMAP, TLS)" error={errors["host"]}>
          <Input value={form.host} onChange={(e) => { set({ host: e.target.value }); }} />
        </Field>
        <Field label="Port" error={errors["port"]}>
          <Input inputMode="numeric" value={form.port} onChange={(e) => { set({ port: e.target.value }); }} />
        </Field>
        <Field label="User name" error={errors["username"]}>
          <Input autoComplete="off" value={form.username} onChange={(e) => { set({ username: e.target.value }); }} />
        </Field>
        <Field label="Password" hint={intake.password_set ? "A password is stored. Leave blank to keep it." : "No password stored yet."}>
          <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => { set({ password: e.target.value }); }} />
        </Field>
        <Field label="Inbox folder">
          <Input value={form.inboxFolder} onChange={(e) => { set({ inboxFolder: e.target.value }); }} />
        </Field>
        <Field label="Folder for handled mail">
          <Input value={form.processedFolder} onChange={(e) => { set({ processedFolder: e.target.value }); }} />
        </Field>
        <Field label="Folder for rejected mail">
          <Input value={form.rejectedFolder} onChange={(e) => { set({ rejectedFolder: e.target.value }); }} />
        </Field>
        <Checkbox
          label="Require the mail server's sender checks (DKIM or SPF) to pass"
          checked={form.requireAuthResults}
          onChange={(e) => { set({ requireAuthResults: e.target.checked }); }}
        />
        <Field label="Trusted checker name (optional)" hint="The Authentication-Results host your mail provider stamps on mail.">
          <Input value={form.authservId} onChange={(e) => { set({ authservId: e.target.value }); }} />
        </Field>
        <Checkbox
          label="Accept mail from each member's own email address"
          checked={form.memberAddresses}
          onChange={(e) => { set({ memberAddresses: e.target.checked }); }}
        />
        <fieldset className="ledger-stack">
          <legend>Extra sender addresses</legend>
          {form.senders.map((sender, index) => (
            <div key={index} className="ledger-actions">
              <Field label={`Address ${String(index + 1)}`}>
                <Input
                  value={sender.address}
                  onChange={(e) => {
                    set({ senders: form.senders.map((s, i) => (i === index ? { ...s, address: e.target.value } : s)) });
                  }}
                />
              </Field>
              <Field label={`Member for address ${String(index + 1)}`}>
                <Select
                  placeholder="Choose a member"
                  options={(members.data ?? []).map((m) => ({ value: m.user_id, label: m.display_name }))}
                  value={sender.userId}
                  onChange={(e) => {
                    set({ senders: form.senders.map((s, i) => (i === index ? { ...s, userId: e.target.value } : s)) });
                  }}
                />
              </Field>
              <Button
                variant="ghost"
                onClick={() => {
                  set({ senders: form.senders.filter((_, i) => i !== index) });
                }}
              >
                {`Remove address ${String(index + 1)}`}
              </Button>
            </div>
          ))}
          {errors["senders"] === undefined ? null : (
            <p role="alert" className="ledger-error">
              {errors["senders"]}
            </p>
          )}
          <div>
            <Button
              onClick={() => {
                set({ senders: [...form.senders, { address: "", userId: "" }] });
              }}
            >
              Add a sender
            </Button>
          </div>
        </fieldset>
        {failure === null ? null : (
          <p role="alert" className="ledger-error">
            {failure}
          </p>
        )}
        <div className="ledger-actions">
          <Button type="submit" variant="primary" loading={busy}>
            Save
          </Button>
          <Button
            loading={test.isPending}
            onClick={() => {
              setTestResult(null);
              test.mutate();
            }}
          >
            Test the connection
          </Button>
        </div>
        {testResult === null ? null : (
          <p role="status" className={testResult.ok ? undefined : "ledger-error"}>
            {describeTest(testResult)}
          </p>
        )}
        <p className="ledger-muted">
          Last check: {when(intake.last_poll_at)}
          {intake.last_poll_outcome === null ? "" : `, ${intake.last_poll_outcome}`}
          {Object.entries(intake.last_poll_counts)
            .filter(([, value]) => typeof value === "number")
            .map(([key, value]) => `, ${key.replaceAll("_", " ")}: ${String(value)}`)
            .join("")}
        </p>
      </form>
    </Card>
  );
}

export function IntakePage() {
  const { session } = useAuthSession();
  const allowed = canManageMembers(session?.household?.role);
  const householdId = session?.household?.id ?? "";
  const intake = useQuery({ queryKey: intakeKey, queryFn: getIntake, enabled: allowed });
  return (
    <SettingsPage title="Intake">
      {!allowed ? (
        <Card>
          <p>Only a household owner or administrator can change how files and mail arrive.</p>
        </Card>
      ) : null}
      {allowed && intake.isPending ? <Spinner label="Loading intake settings" /> : null}
      {intake.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(intake.error)}
        </p>
      ) : null}
      {intake.data === undefined ? null : (
        <>
          <MailboxForm key={intake.data.alias_rotated_at ?? "none"} intake={intake.data} householdId={householdId} />
          <AliasCard intake={intake.data} />
        </>
      )}
      <Card title="Drop folder">
        <p>
          Files placed in the household's drop folder are read like uploads. The first level of subfolders names whose file it
          is. A file in an unrecognised subfolder is held in Review (&ldquo;Unknown owner&rdquo;), where you can choose the
          member and ask PerFin to remember the subfolder.
        </p>
        <p className="ledger-muted">
          The folder's location and live status are set by the server and are not yet shown here.
        </p>
      </Card>
      <Card title="Virus scanning">
        <p>
          Every incoming file is scanned before it is read. A file the scanner flags is rejected and kept out of the library;
          it appears there as &ldquo;Rejected&rdquo; with the reason.
        </p>
        <p className="ledger-muted">The scanner's connection status is not yet shown here.</p>
      </Card>
    </SettingsPage>
  );
}
