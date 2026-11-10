// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Users of this PerFin instance (`/admin/users`, docs/AUTH.md "instance
 * admin"). Only an instance administrator sees the page. They list every
 * account, create a local one (a sign-in with no household yet), disable or
 * enable an account, set the flags the server lets them change (break-glass,
 * email verified) and reset another user's second factors.
 *
 * Every change needs step-up. The page applies the server's rules so it does
 * not offer what would be refused: nobody changes their own account here,
 * break-glass needs a local password and an email can only be vouched for
 * when there is one. The instance administrator flag has no API at all.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Checkbox } from "../../components/Checkbox";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { isApiError } from "../../lib/api/client";
import { authErrorMessage, fieldErrorsOf, passwordProblem } from "../auth/errors";
import { useAuthSession } from "../auth/session";
import { isStepUpCancelled, useStepUp } from "../auth/StepUp";
import { SettingsPage, when } from "./SettingsLayout";
import { createUser, getUser, listUsers, resetUserMfa, updateUser, userKeys, type AdminUser, type AdminUserUpdate } from "./usersApi";

/** The server's username rule (backend `USERNAME_PATTERN`), after trimming and lower-casing. */
const USERNAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

function factorLabel(method: AdminUser["mfa_methods"][number]): string {
  switch (method) {
    case "totp":
      return "authenticator app";
    case "webauthn":
      return "security key";
  }
}

/** "Password, authenticator app and security key": how the account signs in. */
export function signInSummary(user: Pick<AdminUser, "has_password" | "mfa_methods">): string {
  const factors = [...new Set(user.mfa_methods)].map(factorLabel);
  const first = user.has_password ? "Password" : "No password (single sign-on only)";
  return factors.length === 0 ? `${first}, no second factor` : `${first}, ${factors.join(" and ")}`;
}

export function isLocked(user: Pick<AdminUser, "locked_until">, now: Date = new Date()): boolean {
  return user.locked_until !== null && new Date(user.locked_until).getTime() > now.getTime();
}

interface NewUserForm {
  username: string;
  displayName: string;
  email: string;
  password: string;
  confirm: string;
}

const EMPTY_FORM: NewUserForm = { username: "", displayName: "", email: "", password: "", confirm: "" };

/** What is wrong with the form, by field (the server checks all of it again). */
export function newUserProblems(form: NewUserForm): Record<string, string> {
  const found: Record<string, string> = {};
  const username = form.username.trim().toLowerCase();
  if (username === "") {
    found["username"] = "Enter a username.";
  } else if (!USERNAME_RE.test(username)) {
    found["username"] = "Use up to 64 lower-case letters, digits, dots, underscores or hyphens, starting with a letter or digit.";
  }
  if (form.displayName.trim() === "") {
    found["display_name"] = "Enter the person's name.";
  }
  const problem = passwordProblem(form.password, form.confirm);
  if (problem !== null) {
    found[form.password !== form.confirm && form.password.length >= 12 && form.password.length <= 128 ? "confirm" : "password"] = problem;
  } else if (form.password.trim().toLowerCase() === username) {
    found["password"] = "The password must not be the username.";
  }
  return found;
}

function CreateUserForm({ onCreated }: { onCreated: (user: AdminUser) => void }) {
  const runWithStepUp = useStepUp();
  const [form, setForm] = useState<NewUserForm>(EMPTY_FORM);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<NewUserForm>): void => {
    setForm({ ...form, ...patch });
  };

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const found = newUserProblems(form);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    const email = form.email.trim();
    const body = {
      username: form.username.trim().toLowerCase(),
      display_name: form.displayName.trim(),
      password: form.password,
      ...(email === "" ? {} : { email }),
    };
    setBusy(true);
    setFailure(null);
    try {
      const created = await runWithStepUp(() => createUser(body));
      setForm(EMPTY_FORM);
      onCreated(created);
    } catch (caught) {
      if (isStepUpCancelled(caught)) {
        return;
      }
      if (isApiError(caught) && caught.slug === "username-taken") {
        setErrors({ username: "That username is already in use. Choose another one." });
      } else if (isApiError(caught) && caught.slug === "weak-password") {
        setErrors({ password: authErrorMessage(caught) });
      } else {
        setErrors(fieldErrorsOf(caught));
        setFailure(authErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="auth-stack"
      noValidate
      aria-label="Create a user"
      onSubmit={(event) => {
        void submit(event);
      }}
    >
      <Field
        label="Username"
        hint="What they type to sign in. Lower-case letters, digits, dots, underscores and hyphens."
        error={errors["username"]}
        required
      >
        <Input
          name="new-user-username"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={form.username}
          onChange={(event) => {
            set({ username: event.target.value });
          }}
        />
      </Field>
      <Field label="Name" hint="Shown to the other people in their household." error={errors["display_name"]} required>
        <Input
          name="new-user-display-name"
          autoComplete="off"
          value={form.displayName}
          onChange={(event) => {
            set({ displayName: event.target.value });
          }}
        />
      </Field>
      <Field label="Email" hint="Optional; used for password reset." error={errors["email"]}>
        <Input
          name="new-user-email"
          type="email"
          autoComplete="off"
          value={form.email}
          onChange={(event) => {
            set({ email: event.target.value });
          }}
        />
      </Field>
      <Field
        label="Initial password"
        hint="12 to 128 characters, not the username, and not a password known from a data breach. Tell them privately; they can change it under Security after signing in."
        error={errors["password"]}
        required
      >
        <Input
          name="new-user-password"
          type="password"
          autoComplete="new-password"
          value={form.password}
          onChange={(event) => {
            set({ password: event.target.value });
          }}
        />
      </Field>
      <Field label="Repeat the password" error={errors["confirm"]} required>
        <Input
          name="new-user-password-confirm"
          type="password"
          autoComplete="new-password"
          value={form.confirm}
          onChange={(event) => {
            set({ confirm: event.target.value });
          }}
        />
      </Field>
      {failure === null ? null : (
        <p className="auth-error" role="alert">
          {failure}
        </p>
      )}
      <div className="auth-actions">
        <Button type="submit" variant="primary" loading={busy}>
          Create user
        </Button>
      </div>
    </form>
  );
}

function EditUserDialog({ user, onClose, onSaved }: { user: AdminUser; onClose: () => void; onSaved: (user: AdminUser) => void }) {
  const runWithStepUp = useStepUp();
  // Read the account again so the boxes start from what is stored now, not from an old list.
  const fresh = useQuery({ queryKey: userKeys.one(user.id), queryFn: () => getUser(user.id), staleTime: 0, gcTime: 0 });
  const current = fresh.data;
  const [breakglass, setBreakglass] = useState<boolean | null>(null);
  const [verified, setVerified] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isBreakglass = breakglass ?? current?.is_breakglass ?? false;
  const isVerified = verified ?? current?.email_verified ?? false;

  const save = async (): Promise<void> => {
    if (current === undefined) {
      return;
    }
    const body: AdminUserUpdate = {
      ...(isBreakglass === current.is_breakglass ? {} : { is_breakglass: isBreakglass }),
      ...(isVerified === current.email_verified ? {} : { email_verified: isVerified }),
    };
    if (Object.keys(body).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSaved(await runWithStepUp(() => updateUser(user.id, body)));
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(authErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={`Edit ${user.display_name}`}
      description="The name, email address and password belong to the person: they change them in their own settings."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy} disabled={current === undefined} onClick={() => void save()}>
            Save
          </Button>
        </>
      }
    >
      {fresh.isPending ? <Spinner label="Loading the account" /> : null}
      {fresh.isError ? (
        <p className="auth-error" role="alert">
          {authErrorMessage(fresh.error)}
        </p>
      ) : null}
      {current === undefined ? null : (
        <div className="auth-stack">
          <Checkbox
            label="Email address checked"
            hint={
              current.email === null
                ? "This account has no email address, so there is nothing to vouch for."
                : `You vouch that ${current.email} belongs to them. Single sign-on only links an existing account by an email address that is marked as checked.`
            }
            checked={isVerified}
            disabled={current.email === null && !current.email_verified}
            onChange={(event) => {
              setVerified(event.target.checked);
            }}
          />
          <Checkbox
            label="Break-glass account"
            hint={
              current.has_password
                ? "For emergencies when single sign-on is down: the account may sign in with its password, but only from the server's allowlisted network. It cannot hold personal access tokens. Changing this signs the account out everywhere and revokes its tokens."
                : "A break-glass account needs a local password, and this account has none."
            }
            checked={isBreakglass}
            disabled={!current.has_password && !current.is_breakglass}
            onChange={(event) => {
              setBreakglass(event.target.checked);
            }}
          />
          <p className="auth-muted">
            Whether someone is an instance administrator cannot be changed here; that is set on the server itself.
          </p>
          {error === null ? null : (
            <p className="auth-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}

interface Confirming {
  action: "disable" | "reset";
  user: AdminUser;
}

function UsersAdmin({ callerId, needsFactor }: { callerId: string; needsFactor: boolean }) {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const users = useQuery({ queryKey: userKeys.list, queryFn: listUsers });
  const [created, setCreated] = useState<AdminUser | null>(null);
  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [confirming, setConfirming] = useState<Confirming | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const refresh = (): Promise<void> => queryClient.invalidateQueries({ queryKey: userKeys.all });

  const enable = async (user: AdminUser): Promise<void> => {
    setBusyId(user.id);
    try {
      await runWithStepUp(() => updateUser(user.id, { is_active: true }));
      notify({ tone: "success", title: `${user.display_name} can sign in again` });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        notify({ tone: "error", title: "Account not enabled", description: authErrorMessage(caught) });
      }
    } finally {
      setBusyId(null);
      await refresh();
    }
  };

  const confirm = async (): Promise<void> => {
    if (confirming === null) {
      return;
    }
    const { action, user } = confirming;
    setBusyId(user.id);
    setConfirmError(null);
    try {
      if (action === "disable") {
        await runWithStepUp(() => updateUser(user.id, { is_active: false }));
        notify({ tone: "success", title: `${user.display_name} disabled`, description: "They were signed out and their access tokens revoked." });
      } else {
        const result = await runWithStepUp(() => resetUserMfa(user.id));
        notify({
          tone: "success",
          title: `Second factors of ${user.display_name} reset`,
          description:
            result.factors_removed === 0
              ? "They had none. They were signed out everywhere."
              : "Their authenticator apps, security keys and recovery codes were removed, and they were signed out everywhere.",
        });
      }
      setConfirming(null);
      await refresh();
    } catch (caught) {
      if (isStepUpCancelled(caught)) {
        setConfirming(null);
      } else {
        setConfirmError(authErrorMessage(caught));
      }
    } finally {
      setBusyId(null);
    }
  };

  const columns: readonly TableColumn<AdminUser>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      cell: (user) => (
        <>
          {user.display_name} <span className="auth-muted">({user.username})</span>
          {user.id === callerId ? (
            <>
              {" "}
              <Badge tone="info">You</Badge>
            </>
          ) : null}
          {user.is_instance_admin ? (
            <>
              {" "}
              <Badge tone="neutral">Instance admin</Badge>
            </>
          ) : null}
          {user.is_breakglass ? (
            <>
              {" "}
              <Badge tone="neutral">Break-glass</Badge>
            </>
          ) : null}
          {user.is_active ? null : (
            <>
              {" "}
              <Badge tone="warning">Disabled</Badge>
            </>
          )}
          {isLocked(user) ? (
            <>
              {" "}
              <Badge tone="warning">Locked until {when(user.locked_until)}</Badge>
            </>
          ) : null}
        </>
      ),
    },
    {
      id: "email",
      header: "Email",
      cell: (user) =>
        user.email === null ? (
          <span className="auth-muted">None</span>
        ) : (
          <>
            {user.email} <span className="auth-muted">({user.email_verified ? "checked" : "not checked"})</span>
          </>
        ),
    },
    { id: "signin", header: "Signs in with", cell: signInSummary },
    { id: "last", header: "Last sign-in", cell: (user) => when(user.last_login_at) },
    { id: "created", header: "Created", cell: (user) => when(user.created_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (user) =>
        user.id === callerId ? (
          <span className="auth-muted">Change your own account under Profile and Security.</span>
        ) : (
          <span className="auth-actions">
            <Button
              size="sm"
              aria-label={`Edit ${user.display_name}`}
              onClick={() => {
                setEditing(user);
              }}
            >
              Edit
            </Button>
            {user.is_active ? (
              <Button
                size="sm"
                aria-label={`Disable ${user.display_name}`}
                onClick={() => {
                  setConfirmError(null);
                  setConfirming({ action: "disable", user });
                }}
              >
                Disable
              </Button>
            ) : (
              <Button size="sm" aria-label={`Enable ${user.display_name}`} loading={busyId === user.id} onClick={() => void enable(user)}>
                Enable
              </Button>
            )}
            <Button
              size="sm"
              variant="danger"
              aria-label={`Reset second factors of ${user.display_name}`}
              onClick={() => {
                setConfirmError(null);
                setConfirming({ action: "reset", user });
              }}
            >
              Reset second factors
            </Button>
          </span>
        ),
    },
  ];

  const target = confirming?.user.display_name ?? "this user";
  return (
    <>
      {needsFactor ? (
        <Card>
          <p role="note">
            Every change on this page asks for your second factor, and your account has none yet. Set one up under{" "}
            <Link to="/settings/security">Security</Link> first; until then you can only look.
          </p>
        </Card>
      ) : null}
      {created === null ? null : (
        <Card title={`${created.display_name} can now sign in`}>
          <div className="auth-stack" role="status">
            <p>
              The account <strong>{created.username}</strong> exists and signs in with the password you set. It is not in any
              household yet, so they will see no finances until someone adds them to one.
            </p>
            <p>
              Next step: add them to a household under{" "}
              <Link to="/settings/household">Household members</Link>. Choose &ldquo;Someone with a PerFin account&rdquo;, enter
              the username <strong>{created.username}</strong> and pick their role. An owner or admin of another household does
              the same from that household.
            </p>
            <div className="auth-actions">
              <Button
                variant="ghost"
                onClick={() => {
                  setCreated(null);
                }}
              >
                Hide this note
              </Button>
            </div>
          </div>
        </Card>
      )}
      <Card title="Everyone with a sign-in">
        <div className="auth-stack">
          {users.isPending ? <Spinner label="Loading users" /> : null}
          {users.isError ? (
            <p className="auth-error" role="alert">
              {authErrorMessage(users.error)}
            </p>
          ) : null}
          {users.isSuccess ? (
            <Table
              caption="Users of this PerFin instance"
              captionHidden
              columns={columns}
              rows={users.data}
              rowKey={(user) => user.id}
              maxHeight="none"
            />
          ) : null}
          <p className="auth-muted">
            Every change here asks for your second factor first. Which household a person belongs to, and their role in it, is
            set under <Link to="/settings/household">Household members</Link>.
          </p>
        </div>
      </Card>
      <Card title="Create a user">
        <div className="auth-stack">
          <p>
            A local account that signs in with a username and password. It starts in no household, and is never an instance
            administrator.
          </p>
          <CreateUserForm
            onCreated={(user) => {
              setCreated(user);
              notify({ tone: "success", title: `${user.display_name} created` });
              void refresh();
            }}
          />
        </div>
      </Card>
      {editing === null ? null : (
        <EditUserDialog
          user={editing}
          onClose={() => {
            setEditing(null);
          }}
          onSaved={(saved) => {
            setEditing(null);
            notify({ tone: "success", title: `${saved.display_name} saved` });
            void refresh();
          }}
        />
      )}
      <Dialog
        open={confirming !== null}
        size="sm"
        onClose={() => {
          setConfirming(null);
        }}
        title={confirming?.action === "reset" ? `Reset the second factors of ${target}?` : `Disable ${target}?`}
        description={
          confirming?.action === "reset"
            ? "Every authenticator app, security key and recovery code of the account is removed, and they are signed out everywhere. Their password stays. They set up a second factor again after signing in. Use this when they have lost their phone or key."
            : "They are signed out everywhere at once and their personal access tokens are revoked for good. Their data and household memberships stay, and you can enable the account again later."
        }
        footer={
          <>
            <Button
              onClick={() => {
                setConfirming(null);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={confirming !== null && busyId === confirming.user.id}
              onClick={() => {
                void confirm();
              }}
            >
              {confirming?.action === "reset" ? "Reset second factors" : "Disable account"}
            </Button>
          </>
        }
      >
        {confirmError === null ? null : (
          <p className="auth-error" role="alert">
            {confirmError}
          </p>
        )}
      </Dialog>
    </>
  );
}

export function UsersPage() {
  const { session } = useAuthSession();
  if (session === null) {
    return (
      <SettingsPage title="Users">
        <Spinner label="Loading" />
      </SettingsPage>
    );
  }
  if (!session.user.is_instance_admin) {
    return (
      <SettingsPage title="Users">
        <Card>
          <p>
            Only an administrator of this PerFin instance can manage users. To add someone to your household, an owner or admin
            uses <Link to="/settings/household">Household members</Link>.
          </p>
        </Card>
      </SettingsPage>
    );
  }
  return (
    <SettingsPage title="Users">
      <UsersAdmin callerId={session.user.id} needsFactor={!session.mfa.enrolled && session.auth_method !== "oidc"} />
    </SettingsPage>
  );
}
