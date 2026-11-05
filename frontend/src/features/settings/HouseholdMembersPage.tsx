// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Household members (`/households/{id}/members`, docs/API.md "Household
 * notes"). Everyone in the household sees the list; owners and admins add
 * members, change roles and remove members (each change needs step-up).
 * The page applies the server's rules so it never offers what the server
 * would refuse: only an owner appoints, changes or removes an owner, and
 * nobody changes or removes their own membership. The server enforces all
 * of it again.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Select, type SelectOption } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { Table, type TableColumn } from "../../components/Table";
import { useToast } from "../../components/Toast";
import { authErrorMessage, fieldErrorsOf, passwordProblem } from "../auth/errors";
import { canManageMembers, useAuthSession, type Role } from "../auth/session";
import { isStepUpCancelled, useStepUp } from "../auth/StepUp";
import { addMember, changeMemberRole, listMembers, removeMember, settingsKeys, type Member, type MemberAdd } from "./api";
import { SettingsPage, when } from "./SettingsLayout";

function roleLabel(role: Role): string {
  switch (role) {
    case "owner":
      return "Owner";
    case "admin":
      return "Admin";
    case "member":
      return "Member";
    case "viewer":
      return "Viewer";
  }
}

const ROLE_ORDER: readonly Role[] = ["owner", "admin", "member", "viewer"];

/** Roles the caller may give someone. */
export function assignableRoles(callerRole: Role): Role[] {
  if (callerRole === "owner") {
    return [...ROLE_ORDER];
  }
  return callerRole === "admin" ? ["admin", "member", "viewer"] : [];
}

/** Whether the caller may change or remove this member. */
export function canEditMember(callerRole: Role, callerId: string, member: Member): boolean {
  if (!canManageMembers(callerRole) || member.user_id === callerId) {
    return false;
  }
  return member.role !== "owner" || callerRole === "owner";
}

function isRole(value: string): value is Role {
  return (ROLE_ORDER as readonly string[]).includes(value);
}

function roleOptions(roles: readonly Role[]): SelectOption[] {
  return roles.map((role) => ({ value: role, label: roleLabel(role) }));
}

type AddMode = "existing" | "new";

function AddMemberForm({ householdId, callerRole, onAdded }: { householdId: string; callerRole: Role; onAdded: (member: Member) => void }) {
  const runWithStepUp = useStepUp();
  const modeName = useId();
  const [mode, setMode] = useState<AddMode>("existing");
  const [role, setRole] = useState<Role>("member");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    const found: Record<string, string> = {};
    const name = username.trim().toLowerCase();
    if (name === "") {
      found["username"] = "Enter a username.";
    }
    let body: MemberAdd;
    if (mode === "existing") {
      body = { role, username: name };
    } else {
      if (displayName.trim() === "") {
        found["new_user.display_name"] = "Enter the person's name.";
      }
      const problem = passwordProblem(password, confirm);
      if (problem !== null) {
        found[password !== confirm && password.length >= 12 ? "confirm" : "new_user.password"] = problem;
      }
      body = {
        role,
        new_user: {
          username: name,
          display_name: displayName.trim(),
          password,
          ...(email.trim() === "" ? {} : { email: email.trim() }),
        },
      };
    }
    setErrors(found);
    if (Object.keys(found).length > 0) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const added = await runWithStepUp(() => addMember(householdId, body));
      setUsername("");
      setDisplayName("");
      setEmail("");
      setPassword("");
      setConfirm("");
      onAdded(added);
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        setError(authErrorMessage(caught));
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
      aria-label="Add a member"
      onSubmit={(event) => {
        void onSubmit(event);
      }}
    >
      <fieldset className="scope-grid">
        <legend>Who</legend>
        <label>
          <input
            type="radio"
            name={modeName}
            value="existing"
            checked={mode === "existing"}
            onChange={() => {
              setMode("existing");
            }}
          />{" "}
          Someone with a PerFin account
        </label>
        <label>
          <input
            type="radio"
            name={modeName}
            value="new"
            checked={mode === "new"}
            onChange={() => {
              setMode("new");
            }}
          />{" "}
          Create a new account
        </label>
      </fieldset>
      <Field label="Username" error={errors["username"] ?? errors["new_user.username"]} required>
        <Input
          name="member-username"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={username}
          onChange={(event) => {
            setUsername(event.target.value);
          }}
        />
      </Field>
      {mode === "new" ? (
        <>
          <Field label="Name" error={errors["new_user.display_name"]} required>
            <Input
              name="member-display-name"
              value={displayName}
              onChange={(event) => {
                setDisplayName(event.target.value);
              }}
            />
          </Field>
          <Field label="Email" hint="Optional; used for password reset." error={errors["new_user.email"]}>
            <Input
              name="member-email"
              type="email"
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
              }}
            />
          </Field>
          <Field label="Initial password" hint="12 to 128 characters. Share it privately." error={errors["new_user.password"]} required>
            <Input
              name="member-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
            />
          </Field>
          <Field label="Repeat the password" error={errors["confirm"]} required>
            <Input
              name="member-password-confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => {
                setConfirm(event.target.value);
              }}
            />
          </Field>
        </>
      ) : null}
      <Field label="Role" error={errors["role"]}>
        <Select
          name="member-role"
          options={roleOptions(assignableRoles(callerRole))}
          value={role}
          onChange={(event) => {
            const { value } = event.target;
            if (isRole(value)) {
              setRole(value);
            }
          }}
        />
      </Field>
      {error === null ? null : (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      <div className="auth-actions">
        <Button type="submit" variant="primary" loading={busy}>
          Add member
        </Button>
      </div>
    </form>
  );
}

export function HouseholdMembersPage() {
  const queryClient = useQueryClient();
  const runWithStepUp = useStepUp();
  const { notify } = useToast();
  const { session } = useAuthSession();
  const household = session?.household ?? null;
  const householdId = household?.id ?? "";
  const members = useQuery({
    queryKey: settingsKeys.members(householdId),
    queryFn: () => listMembers(householdId),
    enabled: household !== null,
  });
  const [removing, setRemoving] = useState<Member | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);

  if (session === null) {
    return (
      <SettingsPage title="Household members">
        <Spinner label="Loading" />
      </SettingsPage>
    );
  }
  if (household === null) {
    return (
      <SettingsPage title="Household members">
        <Card>
          <p>You are not a member of a household yet. Ask an administrator to add you to one.</p>
        </Card>
      </SettingsPage>
    );
  }

  const callerRole = household.role;
  const callerId = session.user.id;
  const manage = canManageMembers(callerRole);
  const refresh = (): Promise<void> => queryClient.invalidateQueries({ queryKey: settingsKeys.members(householdId) });

  const changeRole = async (member: Member, role: Role): Promise<void> => {
    if (role === member.role) {
      return;
    }
    setBusyId(member.user_id);
    try {
      await runWithStepUp(() => changeMemberRole(householdId, member.user_id, role));
      notify({ tone: "success", title: `${member.display_name} is now ${roleLabel(role).toLowerCase()}` });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        notify({ tone: "error", title: "Role not changed", description: authErrorMessage(caught) });
      }
    } finally {
      setBusyId(null);
      await refresh();
    }
  };

  const confirmRemove = async (): Promise<void> => {
    if (removing === null) {
      return;
    }
    const member = removing;
    setBusyId(member.user_id);
    setRemoveError(null);
    try {
      await runWithStepUp(() => removeMember(householdId, member.user_id));
      setRemoving(null);
      notify({ tone: "success", title: `${member.display_name} removed` });
      await refresh();
    } catch (caught) {
      if (isStepUpCancelled(caught)) {
        setRemoving(null);
      } else {
        setRemoveError(authErrorMessage(caught));
      }
    } finally {
      setBusyId(null);
    }
  };

  const columns: readonly TableColumn<Member>[] = [
    {
      id: "name",
      header: "Name",
      rowHeader: true,
      cell: (member) => (
        <>
          {member.display_name} <span className="auth-muted">({member.username})</span>
          {member.user_id === callerId ? (
            <>
              {" "}
              <Badge tone="info">You</Badge>
            </>
          ) : null}
          {member.is_active ? null : (
            <>
              {" "}
              <Badge tone="warning">Disabled</Badge>
            </>
          )}
        </>
      ),
    },
    {
      id: "role",
      header: "Role",
      cell: (member) =>
        canEditMember(callerRole, callerId, member) ? (
          <Select
            aria-label={`Role of ${member.display_name}`}
            options={roleOptions(assignableRoles(callerRole))}
            value={member.role}
            disabled={busyId === member.user_id}
            onChange={(event) => {
              const { value } = event.target;
              if (isRole(value)) {
                void changeRole(member, value);
              }
            }}
          />
        ) : (
          roleLabel(member.role)
        ),
    },
    { id: "joined", header: "Joined", cell: (member) => when(member.joined_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (member) =>
        canEditMember(callerRole, callerId, member) ? (
          <Button
            size="sm"
            variant="danger"
            aria-label={`Remove ${member.display_name}`}
            onClick={() => {
              setRemoveError(null);
              setRemoving(member);
            }}
          >
            Remove
          </Button>
        ) : null,
    },
  ];

  return (
    <SettingsPage title="Household members">
      <Card title={household.name}>
        <p>
          Your role: <strong>{roleLabel(callerRole)}</strong>.{" "}
          {manage ? "You can add members and change their roles." : "Only owners and admins can change members."}
        </p>
        {members.isPending ? <Spinner label="Loading members" /> : null}
        {members.isError ? (
          <p className="auth-error" role="alert">
            {authErrorMessage(members.error)}
          </p>
        ) : null}
        {members.isSuccess ? (
          <Table
            caption={`Members of ${household.name}`}
            captionHidden
            columns={columns}
            rows={members.data}
            rowKey={(member) => member.user_id}
            maxHeight="none"
          />
        ) : null}
      </Card>
      {manage ? (
        <Card title="Add a member">
          <AddMemberForm
            householdId={householdId}
            callerRole={callerRole}
            onAdded={(member) => {
              notify({ tone: "success", title: `${member.display_name} added as ${roleLabel(member.role).toLowerCase()}` });
              void refresh();
            }}
          />
        </Card>
      ) : null}
      <Dialog
        open={removing !== null}
        size="sm"
        onClose={() => {
          setRemoving(null);
        }}
        title={`Remove ${removing?.display_name ?? "member"}?`}
        description="They lose access to this household at once; their sessions and tokens here end."
        footer={
          <>
            <Button
              onClick={() => {
                setRemoving(null);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={removing !== null && busyId === removing.user_id}
              onClick={() => {
                void confirmRemove();
              }}
            >
              Remove
            </Button>
          </>
        }
      >
        {removeError === null ? null : (
          <p className="auth-error" role="alert">
            {removeError}
          </p>
        )}
      </Dialog>
    </SettingsPage>
  );
}
