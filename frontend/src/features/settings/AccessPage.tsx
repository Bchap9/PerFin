// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Account access per member: which accounts a member or viewer may see or
 * edit. Owners and admins see every account, so they have no grants. Each
 * change needs step-up (SECURITY.md I2).
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Table, type TableColumn } from "../../components/Table";
import { queryRoots } from "../../lib/queryKeys";
import { canManageMembers, useAuthSession } from "../auth/session";
import { ErrorLine, QueryState } from "../planning/PlanningPage";
import { useAccounts, type Account } from "../planning/queries";
import { useAction } from "../planning/useAction";
import { listMembers, settingsKeys } from "./api";
import { dataKeys, listMemberGrants, revokeMemberGrant, setMemberGrant, type GrantLevel } from "./dataApi";
import { SettingsPage } from "./SettingsLayout";

type Choice = GrantLevel | "none";

/** Levels a member may be given: viewers never get `edit`. */
export function levelChoices(role: "member" | "viewer"): { value: Choice; label: string }[] {
  const choices: { value: Choice; label: string }[] = [
    { value: "none", label: "No access" },
    { value: "view", label: "Can view" },
  ];
  return role === "viewer" ? choices : [...choices, { value: "edit", label: "Can edit" }];
}

function isChoice(value: string): value is Choice {
  return value === "none" || value === "view" || value === "edit";
}

function GrantsTable({ householdId, userId, role }: { householdId: string; userId: string; role: "member" | "viewer" }) {
  const accounts = useAccounts();
  const grants = useQuery({ queryKey: dataKeys.grants(householdId, userId), queryFn: () => listMemberGrants(householdId, userId) });
  const change = useAction(
    async (accountId: string, level: Choice): Promise<void> => {
      if (level === "none") {
        await revokeMemberGrant(householdId, userId, accountId);
      } else {
        await setMemberGrant(householdId, userId, accountId, level);
      }
    },
    { stepUp: true, success: "Access updated", invalidate: [dataKeys.grants(householdId, userId), queryRoots.accounts] },
  );
  const levelOf = (accountId: string): Choice => grants.data?.find((grant) => grant.account_id === accountId)?.level ?? "none";
  const columns: TableColumn<Account>[] = [
    { id: "name", header: "Account", rowHeader: true, cell: (account) => account.nickname ?? account.name },
    {
      id: "level",
      header: "Access",
      cell: (account) => (
        <Field label={`Access to ${account.nickname ?? account.name}`} className="field--inline">
          <Select
            value={levelOf(account.id)}
            options={levelChoices(role)}
            onChange={(event) => {
              if (isChoice(event.target.value)) {
                void change.run(account.id, event.target.value);
              }
            }}
          />
        </Field>
      ),
    },
  ];
  return (
    <QueryState loading={accounts.isPending || grants.isPending} error={accounts.error ?? grants.error} label="account access">
      <ErrorLine message={change.error} />
      <Table
        caption="Account access"
        captionHidden
        columns={columns}
        rows={(accounts.data ?? []).filter((account) => !account.is_closed)}
        rowKey={(account) => account.id}
        maxHeight="none"
        empty="There are no accounts yet."
      />
    </QueryState>
  );
}

export function AccessPage() {
  const { session } = useAuthSession();
  const householdId = session?.household?.id ?? null;
  const allowed = canManageMembers(session?.household?.role);
  const members = useQuery({
    queryKey: settingsKeys.members(householdId ?? "none"),
    enabled: householdId !== null && allowed,
    queryFn: () => listMembers(householdId ?? ""),
  });
  const [userId, setUserId] = useState("");
  const restricted = (members.data ?? []).filter((member) => member.role === "member" || member.role === "viewer");
  const chosen = restricted.find((member) => member.user_id === userId);
  return (
    <SettingsPage title="Account access">
      {!allowed || householdId === null ? (
        <p>Only an owner or admin can change who sees which account.</p>
      ) : (
        <QueryState loading={members.isPending} error={members.error} label="members">
          <p className="muted">
            Members and viewers see only the accounts you share with them. Owners and admins always see every account.
          </p>
          {restricted.length === 0 ? (
            <p>There are no members or viewers to share accounts with.</p>
          ) : (
            <>
              <Field label="Member">
                <Select
                  value={userId}
                  placeholder="Choose a member"
                  options={restricted.map((member) => ({ value: member.user_id, label: `${member.display_name} (${member.role})` }))}
                  onChange={(event) => { setUserId(event.target.value); }}
                />
              </Field>
              {chosen === undefined || (chosen.role !== "member" && chosen.role !== "viewer") ? null : (
                <GrantsTable key={chosen.user_id} householdId={householdId} userId={chosen.user_id} role={chosen.role} />
              )}
            </>
          )}
        </QueryState>
      )}
    </SettingsPage>
  );
}
