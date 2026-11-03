// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Personal access tokens (SECURITY.md I3): create with a name, scopes from
 * what the caller's role may grant and a mandatory expiry (step-up; the
 * token is shown once), list, and revoke. A token acts only in the current
 * household.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Badge, type BadgeTone } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Checkbox } from "../../../components/Checkbox";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { Spinner } from "../../../components/Spinner";
import { Table, type TableColumn } from "../../../components/Table";
import { useToast } from "../../../components/Toast";
import { isApiError } from "../../../lib/api/client";
import { authErrorMessage, fieldErrorsOf } from "../../auth/errors";
import { ShowOnceDialog } from "../../auth/ShowOnceDialog";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";
import { createToken, getScopeCatalog, listTokens, revokeToken, settingsKeys, type ScopeCatalog, type Token } from "../api";
import { when } from "../SettingsLayout";

const DEFAULT_DAYS = 30;

const STATUS_TONES: Readonly<Record<Token["status"], BadgeTone>> = {
  active: "success",
  expired: "neutral",
  revoked: "danger",
};

const STATUS_LABELS: Readonly<Record<Token["status"], string>> = {
  active: "Active",
  expired: "Expired",
  revoked: "Revoked",
};

function tokenError(error: unknown): string {
  if (isApiError(error) && error.slug === "mfa-enrollment-required") {
    return "Set up a second factor in PerFin before creating tokens.";
  }
  if (isApiError(error) && error.slug === "too-many-tokens") {
    return "You have the maximum number of live tokens. Revoke one first.";
  }
  return authErrorMessage(error);
}

function CreateTokenForm({ catalog, onCreated }: { catalog: ScopeCatalog; onCreated: (token: string) => void }) {
  const runWithStepUp = useStepUp();
  const maxDays = catalog.max_expires_in_days;
  const grantable = catalog.scopes.filter((scope) => scope.grantable && !scope.reserved);
  const [name, setName] = useState("");
  const [days, setDays] = useState(String(Math.min(DEFAULT_DAYS, maxDays)));
  const [scopes, setScopes] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const toggle = (scope: string, on: boolean): void => {
    setScopes((previous) => {
      const next = new Set(previous);
      if (on) {
        next.add(scope);
      } else {
        next.delete(scope);
      }
      return next;
    });
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const found: Record<string, string> = {};
    const trimmed = name.trim();
    if (trimmed === "" || trimmed.length > 100) {
      found["name"] = "Give the token a name of 1 to 100 characters.";
    }
    const expires = Number(days);
    if (!Number.isInteger(expires) || expires < 1 || expires > maxDays) {
      found["expires_in_days"] = `Choose 1 to ${String(maxDays)} days.`;
    }
    if (scopes.size === 0) {
      found["scopes"] = "Choose at least one scope.";
    }
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await runWithStepUp(() =>
        createToken({ name: trimmed, scopes: [...scopes].sort(), expires_in_days: expires }),
      );
      setName("");
      setScopes(new Set());
      onCreated(created.token);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(tokenError(caught));
        setErrors(fieldErrorsOf(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="auth-stack"
      noValidate
      aria-label="Create a personal access token"
      onSubmit={(event) => {
        void onSubmit(event);
      }}
    >
      <Field label="Token name" hint="What uses it, for example 'Home Assistant'." error={errors["name"]} required>
        <Input
          name="token-name"
          maxLength={100}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </Field>
      <Field label="Expires after (days)" hint={`At most ${String(maxDays)} days.`} error={errors["expires_in_days"]} required>
        <Input
          name="token-days"
          type="number"
          inputMode="numeric"
          min={1}
          max={maxDays}
          value={days}
          onChange={(event) => {
            setDays(event.target.value);
          }}
        />
      </Field>
      <fieldset className="scope-grid" aria-describedby={errors["scopes"] === undefined ? undefined : "token-scopes-error"}>
        <legend>Scopes</legend>
        {grantable.map((scope) => (
          <Checkbox
            key={scope.scope}
            label={scope.scope}
            hint={scope.description}
            checked={scopes.has(scope.scope)}
            onChange={(event) => {
              toggle(scope.scope, event.target.checked);
            }}
          />
        ))}
      </fieldset>
      {errors["scopes"] === undefined ? null : (
        <p id="token-scopes-error" className="field__error">
          {errors["scopes"]}
        </p>
      )}
      {error === null ? null : (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      <div className="auth-actions">
        <Button type="submit" variant="primary" loading={busy}>
          Create token
        </Button>
      </div>
    </form>
  );
}

export function TokensSection() {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const catalog = useQuery({ queryKey: settingsKeys.scopes, queryFn: getScopeCatalog });
  const tokens = useQuery({ queryKey: settingsKeys.tokens, queryFn: listTokens });
  const [secret, setSecret] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = (): Promise<void> => queryClient.invalidateQueries({ queryKey: settingsKeys.tokens });

  const revoke = async (token: Token): Promise<void> => {
    setBusyId(token.id);
    try {
      await revokeToken(token.id);
      notify({ tone: "success", title: `Token ${token.name} revoked` });
    } catch (caught) {
      notify({ tone: "error", title: "Could not revoke the token", description: tokenError(caught) });
    } finally {
      setBusyId(null);
      await refresh();
    }
  };

  const columns: readonly TableColumn<Token>[] = [
    { id: "name", header: "Name", rowHeader: true, cell: (token) => token.name },
    { id: "prefix", header: "Token", cell: (token) => <code>{`pfn_${token.prefix}_…`}</code> },
    {
      id: "scopes",
      header: "Scopes",
      cell: (token) => (
        <>
          {token.active_scopes.join(", ") || "None"}
          {token.active_scopes.length < token.scopes.length ? (
            <span className="auth-muted"> (narrowed by your current role)</span>
          ) : null}
        </>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: (token) => <Badge tone={STATUS_TONES[token.status]}>{STATUS_LABELS[token.status]}</Badge>,
    },
    { id: "expires", header: "Expires", cell: (token) => when(token.expires_at) },
    { id: "used", header: "Last used", cell: (token) => when(token.last_used_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (token) =>
        token.status === "active" ? (
          <Button
            size="sm"
            variant="danger"
            loading={busyId === token.id}
            aria-label={`Revoke ${token.name}`}
            onClick={() => {
              void revoke(token);
            }}
          >
            Revoke
          </Button>
        ) : null,
    },
  ];

  return (
    <Card title="Personal access tokens">
      <div className="settings-stack">
        <p>Tokens let scripts use the API as you, in this household only, with the scopes you choose.</p>
        {tokens.isPending ? <Spinner label="Loading your tokens" /> : null}
        {tokens.isError ? (
          <p className="auth-error" role="alert">
            {tokenError(tokens.error)}
          </p>
        ) : null}
        {tokens.isSuccess ? (
          <Table
            caption="Your tokens"
            captionHidden
            columns={columns}
            rows={tokens.data}
            rowKey={(token) => token.id}
            empty="No tokens yet."
            maxHeight="md"
            density="compact"
          />
        ) : null}
        <h3 className="auth-subheading">New token</h3>
        {catalog.isPending ? <Spinner label="Loading scopes" /> : null}
        {catalog.isError ? (
          <p className="auth-error" role="alert">
            {tokenError(catalog.error)}
          </p>
        ) : null}
        {catalog.isSuccess ? (
          <CreateTokenForm
            catalog={catalog.data}
            onCreated={(token) => {
              setSecret(token);
              void refresh();
            }}
          />
        ) : null}
      </div>
      <ShowOnceDialog
        open={secret !== null}
        title="Your new token"
        description="Copy it now. PerFin stores only a hash and cannot show it again."
        lines={secret === null ? [] : [secret]}
        label="token"
        onDone={() => {
          setSecret(null);
        }}
      />
    </Card>
  );
}
