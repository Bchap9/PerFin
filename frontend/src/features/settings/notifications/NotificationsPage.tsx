// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Badge } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Checkbox } from "../../../components/Checkbox";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { Select } from "../../../components/Select";
import { Spinner } from "../../../components/Spinner";
import { useToast } from "../../../components/Toast";
import { errorMessage } from "../../../lib/api/client";
import { canManageMembers, useAuthSession } from "../../auth/session";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";
import { SettingsPage, when } from "../SettingsLayout";
import {
  channelsKey,
  createChannel,
  deleteChannel,
  listChannels,
  testChannel,
  updateChannel,
  type Channel,
  type ChannelSaved,
  type ChannelTest,
} from "./api";
import { buildCreate, buildUpdate, describeOutcome, emptyForm, formFrom, KINDS, TYPES, typeLabel, type ChannelForm } from "./form";

function TypeFields({
  form,
  set,
  errors,
  stored,
}: {
  form: ChannelForm;
  set: (patch: Partial<ChannelForm>) => void;
  errors: Record<string, string>;
  stored: readonly string[];
}) {
  const keep = (key: string): string | undefined =>
    stored.includes(key) ? "Stored. Leave blank to keep it (type it again if you change the server)." : undefined;
  switch (form.type) {
    case "smtp":
      return (
        <>
          <Field label="Mail server" error={errors["host"]}>
            <Input value={form.host} onChange={(e) => { set({ host: e.target.value }); }} />
          </Field>
          <Field label="Port" error={errors["port"]}>
            <Input inputMode="numeric" value={form.port} onChange={(e) => { set({ port: e.target.value }); }} />
          </Field>
          <Field label="TLS" hint="Mail is never sent without TLS.">
            <Select
              value={form.tls}
              options={[
                { value: "starttls", label: "STARTTLS (usually port 587)" },
                { value: "implicit", label: "TLS from the start (usually port 465)" },
              ]}
              onChange={(e) => { set({ tls: e.target.value === "implicit" ? "implicit" : "starttls" }); }}
            />
          </Field>
          <Field label="Sender address" error={errors["sender"]}>
            <Input value={form.sender} onChange={(e) => { set({ sender: e.target.value }); }} />
          </Field>
          <Field label="Recipients" hint="Up to ten addresses, separated by commas." error={errors["recipients"]}>
            <Input value={form.recipients} onChange={(e) => { set({ recipients: e.target.value }); }} />
          </Field>
          <Field label="SMTP user name (optional)">
            <Input autoComplete="off" value={form.username} onChange={(e) => { set({ username: e.target.value }); }} />
          </Field>
          <Field label="SMTP password" hint={keep("password")}>
            <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => { set({ password: e.target.value }); }} />
          </Field>
        </>
      );
    case "webhook":
      return (
        <Field
          label="Webhook URL"
          hint="An https:// address. Each POST is signed with HMAC-SHA256; the signing secret is shown once after saving."
          error={errors["url"]}
        >
          <Input value={form.url} onChange={(e) => { set({ url: e.target.value }); }} />
        </Field>
      );
    case "ntfy":
      return (
        <>
          <Field label="ntfy server" error={errors["serverUrl"]}>
            <Input value={form.serverUrl} onChange={(e) => { set({ serverUrl: e.target.value }); }} />
          </Field>
          <Field label="Topic" hint={keep("topic") ?? "Anyone who knows a public topic can read it; pick a long random one."} error={errors["topic"]}>
            <Input type="password" autoComplete="off" value={form.topic} onChange={(e) => { set({ topic: e.target.value }); }} />
          </Field>
          <Field label="Access token (optional)" hint={keep("token")}>
            <Input type="password" autoComplete="off" value={form.token} onChange={(e) => { set({ token: e.target.value }); }} />
          </Field>
          <Field label="Priority (1 to 5)">
            <Input inputMode="numeric" value={form.priority} onChange={(e) => { set({ priority: e.target.value }); }} />
          </Field>
        </>
      );
    case "gotify":
      return (
        <>
          <Field label="Gotify server" error={errors["serverUrl"]}>
            <Input value={form.serverUrl} onChange={(e) => { set({ serverUrl: e.target.value }); }} />
          </Field>
          <Field label="Application token" hint={keep("token")} error={errors["token"]}>
            <Input type="password" autoComplete="off" value={form.token} onChange={(e) => { set({ token: e.target.value }); }} />
          </Field>
          <Field label="Priority (0 to 10)">
            <Input inputMode="numeric" value={form.priority} onChange={(e) => { set({ priority: e.target.value }); }} />
          </Field>
        </>
      );
    case "matrix":
      return (
        <>
          <Field label="Homeserver" error={errors["homeserverUrl"]}>
            <Input value={form.homeserverUrl} onChange={(e) => { set({ homeserverUrl: e.target.value }); }} />
          </Field>
          <Field label="Room id" error={errors["roomId"]}>
            <Input value={form.roomId} onChange={(e) => { set({ roomId: e.target.value }); }} />
          </Field>
          <Field label="Access token" hint={keep("access_token")} error={errors["accessToken"]}>
            <Input type="password" autoComplete="off" value={form.accessToken} onChange={(e) => { set({ accessToken: e.target.value }); }} />
          </Field>
        </>
      );
  }
}

function ChannelEditor({ channel, onDone }: { channel: Channel | null; onDone: (secret: string | null) => void }) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const [form, setForm] = useState<ChannelForm>(() => (channel === null ? emptyForm() : formFrom(channel)));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<ChannelForm>): void => {
    setForm({ ...form, ...patch });
  };
  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setFailure(null);
    let save: () => Promise<ChannelSaved>;
    if (channel === null) {
      const built = buildCreate(form);
      setErrors(built.errors);
      const body = built.body;
      if (body === null) {
        return;
      }
      save = () => createChannel(body);
    } else {
      const built = buildUpdate(form, channel);
      setErrors(built.errors);
      const body = built.body;
      if (body === null) {
        return;
      }
      save = () => updateChannel(channel.id, body);
    }
    setBusy(true);
    try {
      const saved = await runWithStepUp(save);
      notify({ tone: "success", title: channel === null ? "Channel added" : "Channel saved" });
      await queryClient.invalidateQueries({ queryKey: channelsKey });
      onDone(saved.signing_secret ?? null);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setFailure(errorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="ledger-stack"
      aria-label={channel === null ? "New channel" : `Edit ${channel.name}`}
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <Field label="Name" error={errors["name"]}>
        <Input value={form.name} onChange={(e) => { set({ name: e.target.value }); }} />
      </Field>
      {channel === null ? (
        <Field label="Service">
          <Select
            value={form.type}
            options={TYPES.map((t) => ({ value: t.value, label: t.label }))}
            onChange={(e) => {
              const type = TYPES.find((t) => t.value === e.target.value)?.value ?? "ntfy";
              setForm({ ...emptyForm(type), name: form.name, kinds: form.kinds, enabled: form.enabled });
              setErrors({});
            }}
          />
        </Field>
      ) : null}
      <TypeFields form={form} set={set} errors={errors} stored={channel?.secrets_set ?? []} />
      <fieldset className="ledger-stack">
        <legend>Send a notification when</legend>
        {KINDS.map(({ kind, label }) => (
          <Checkbox
            key={kind}
            label={label}
            checked={form.kinds.includes(kind)}
            onChange={(e) => {
              set({ kinds: e.target.checked ? [...form.kinds, kind] : form.kinds.filter((k) => k !== kind) });
            }}
          />
        ))}
      </fieldset>
      <Checkbox label="Enabled" checked={form.enabled} onChange={(e) => { set({ enabled: e.target.checked }); }} />
      {failure === null ? null : (
        <p role="alert" className="ledger-error">
          {failure}
        </p>
      )}
      <div className="ledger-actions">
        <Button type="submit" variant="primary" loading={busy}>
          {channel === null ? "Add channel" : "Save"}
        </Button>
        {channel === null ? null : (
          <Button
            variant="ghost"
            onClick={() => {
              onDone(null);
            }}
          >
            Cancel
          </Button>
        )}
      </div>
    </form>
  );
}

function ChannelRow({ channel, onSecret }: { channel: Channel; onSecret: (secret: string) => void }) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const [editing, setEditing] = useState(false);
  const [result, setResult] = useState<ChannelTest | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (name: string, action: () => Promise<unknown>): Promise<void> => {
    setBusy(name);
    setFailure(null);
    try {
      await action();
      await queryClient.invalidateQueries({ queryKey: channelsKey });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setFailure(errorMessage(caught));
      }
    } finally {
      setBusy(null);
    }
  };
  if (editing) {
    return (
      <Card title={`Edit ${channel.name}`}>
        <ChannelEditor
          channel={channel}
          onDone={() => {
            setEditing(false);
          }}
        />
      </Card>
    );
  }
  return (
    <Card title={channel.name}>
      <p>
        {typeLabel(channel.type)}{" "}
        <Badge tone={channel.enabled ? "success" : "neutral"}>{channel.enabled ? "Enabled" : "Disabled"}</Badge>
      </p>
      <p className="ledger-muted">
        {channel.events.length} of {KINDS.length} kinds. Last sent: {when(channel.last_sent_at)}.{" "}
        {describeOutcome(channel.last_outcome)}
      </p>
      {result === null ? null : (
        <p role="status" className={result.ok ? undefined : "ledger-error"}>
          {`Test: ${describeOutcome(result.outcome)}`}
        </p>
      )}
      {failure === null ? null : (
        <p role="alert" className="ledger-error">
          {failure}
        </p>
      )}
      <div className="ledger-actions">
        <Button
          loading={busy === "test"}
          onClick={() =>
            void run("test", async () => {
              setResult(null);
              setResult(await testChannel(channel.id));
            })
          }
        >
          {`Send a test to ${channel.name}`}
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            setEditing(true);
          }}
        >
          {`Edit ${channel.name}`}
        </Button>
        {channel.type === "webhook" ? (
          <Button
            variant="ghost"
            loading={busy === "rotate"}
            onClick={() =>
              void run("rotate", async () => {
                const saved = await runWithStepUp(() => updateChannel(channel.id, { rotate_secret: true }));
                if (saved.signing_secret !== null && saved.signing_secret !== undefined) {
                  onSecret(saved.signing_secret);
                }
              })
            }
          >
            {`New signing secret for ${channel.name}`}
          </Button>
        ) : null}
        <Button
          variant="danger"
          loading={busy === "delete"}
          onClick={() =>
            void run("delete", async () => {
              await runWithStepUp(() => deleteChannel(channel.id));
              notify({ tone: "success", title: "Channel removed" });
            })
          }
        >
          {`Remove ${channel.name}`}
        </Button>
      </div>
    </Card>
  );
}

export function NotificationsPage() {
  const { session } = useAuthSession();
  const allowed = canManageMembers(session?.household?.role);
  const channels = useQuery({ queryKey: channelsKey, queryFn: listChannels, enabled: allowed });
  const [secret, setSecret] = useState<string | null>(null);
  const [formKey, setFormKey] = useState(0);
  return (
    <SettingsPage title="Notifications">
      {!allowed ? (
        <Card>
          <p>Only a household owner or administrator can set up notifications.</p>
        </Card>
      ) : (
        <Card>
          <p>
            PerFin sends a short, generic sentence (for example &ldquo;A recovery code was used to sign in to a household
            member&apos;s account.&rdquo;). It never includes names, email addresses, amounts, accounts or documents. Sign in
            to PerFin to see the details.
          </p>
        </Card>
      )}
      {allowed && channels.isPending ? <Spinner label="Loading notification channels" /> : null}
      {channels.isError ? (
        <p role="alert" className="ledger-error">
          {errorMessage(channels.error)}
        </p>
      ) : null}
      {secret === null ? null : (
        <Card title="Webhook signing secret">
          <p role="status">
            Copy it now; it is not shown again: <code data-testid="signing-secret">{secret}</code>
          </p>
          <p className="ledger-muted">
            Verify each request: HMAC-SHA256 of the X-PerFin-Timestamp value, a dot and the raw body must equal the hex after
            &ldquo;v1=&rdquo; in X-PerFin-Signature. Refuse old timestamps.
          </p>
          <Button
            onClick={() => {
              setSecret(null);
            }}
          >
            Done
          </Button>
        </Card>
      )}
      {channels.data === undefined ? null : (
        <>
          {channels.data.length === 0 ? <p>No notification channels yet.</p> : null}
          {channels.data.map((channel) => (
            <ChannelRow key={channel.id} channel={channel} onSecret={setSecret} />
          ))}
          <Card title="Add a channel">
            <ChannelEditor
              key={formKey}
              channel={null}
              onDone={(made) => {
                setSecret(made);
                setFormKey(formKey + 1);
              }}
            />
          </Card>
        </>
      )}
    </SettingsPage>
  );
}
