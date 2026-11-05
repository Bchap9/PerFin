// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Signed-in sessions (`GET /auth/sessions`): revoke one, or every session but
 * this one. Revoking this session signs out.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Spinner } from "../../../components/Spinner";
import { Table, type TableColumn } from "../../../components/Table";
import { useToast } from "../../../components/Toast";
import { listSessions, revokeOtherSessions, revokeSession, type SessionInfo } from "../../auth/api";
import { useSignOut } from "../../auth/AccountMenu";
import { authErrorMessage } from "../../auth/errors";
import { settingsKeys } from "../api";
import { when } from "../SettingsLayout";

const METHOD_LABELS = new Map<string, string>(Object.entries({
  local: "Password",
  oidc: "Single sign-on",
  breakglass: "Break-glass",
  bootstrap: "First-run setup",
}));

/** A short device description from the user agent (display only). */
export function describeAgent(agent: string | null): string {
  if (agent === null || agent.trim() === "") {
    return "Unknown device";
  }
  const browser = agent.includes("Firefox/")
    ? "Firefox"
    : agent.includes("Edg/")
      ? "Edge"
      : agent.includes("Chrome/")
        ? "Chrome"
        : agent.includes("Safari/")
          ? "Safari"
          : null;
  const system = agent.includes("Windows")
    ? "Windows"
    : agent.includes("Android")
      ? "Android"
      : /iPhone|iPad/.test(agent)
        ? "iOS"
        : /Mac OS X|Macintosh/.test(agent)
          ? "macOS"
          : agent.includes("Linux")
            ? "Linux"
            : null;
  if (browser === null && system === null) {
    return agent.length > 60 ? `${agent.slice(0, 60)}…` : agent;
  }
  return [browser, system].filter(Boolean).join(" on ");
}

export function SessionsSection() {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const { signOut } = useSignOut();
  const sessions = useQuery({ queryKey: settingsKeys.sessions, queryFn: listSessions });
  const [busyId, setBusyId] = useState<string | null>(null);

  const revoke = async (session: SessionInfo): Promise<void> => {
    if (session.current) {
      await signOut();
      return;
    }
    setBusyId(session.id);
    try {
      await revokeSession(session.id);
      notify({ tone: "success", title: "Session signed out" });
    } catch (caught) {
      notify({ tone: "error", title: "Could not sign out the session", description: authErrorMessage(caught) });
    } finally {
      setBusyId(null);
      await queryClient.invalidateQueries({ queryKey: settingsKeys.sessions });
    }
  };

  const revokeOthers = async (): Promise<void> => {
    setBusyId("others");
    try {
      const count = await revokeOtherSessions();
      notify({ tone: "success", title: `${String(count)} other sessions signed out` });
    } catch (caught) {
      notify({ tone: "error", title: "Could not sign out other sessions", description: authErrorMessage(caught) });
    } finally {
      setBusyId(null);
      await queryClient.invalidateQueries({ queryKey: settingsKeys.sessions });
    }
  };

  const columns: readonly TableColumn<SessionInfo>[] = [
    {
      id: "device",
      header: "Device",
      rowHeader: true,
      cell: (session) => (
        <>
          {describeAgent(session.user_agent)}{" "}
          {session.current ? <Badge tone="info">This device</Badge> : null}
        </>
      ),
    },
    { id: "method", header: "Signed in with", cell: (session) => METHOD_LABELS.get(session.login_method) ?? session.login_method },
    { id: "ip", header: "IP address", cell: (session) => session.ip ?? "Unknown" },
    { id: "created", header: "Signed in", cell: (session) => when(session.created_at) },
    { id: "seen", header: "Last active", cell: (session) => when(session.last_seen_at) },
    { id: "expires", header: "Expires", cell: (session) => when(session.expires_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (session) => (
        <Button
          size="sm"
          variant="secondary"
          loading={busyId === session.id}
          aria-label={session.current ? "Sign out this device" : `Sign out ${describeAgent(session.user_agent)}`}
          onClick={() => {
            void revoke(session);
          }}
        >
          Sign out
        </Button>
      ),
    },
  ];

  const others = (sessions.data ?? []).filter((session) => !session.current).length;

  return (
    <Card
      title="Sessions"
      actions={
        <Button
          size="sm"
          disabled={others === 0}
          loading={busyId === "others"}
          onClick={() => {
            void revokeOthers();
          }}
        >
          Sign out all other sessions
        </Button>
      }
    >
      {sessions.isPending ? <Spinner label="Loading your sessions" /> : null}
      {sessions.isError ? (
        <p className="auth-error" role="alert">
          {authErrorMessage(sessions.error)}
        </p>
      ) : null}
      {sessions.isSuccess ? (
        <Table
          caption="Where you are signed in"
          captionHidden
          columns={columns}
          rows={sessions.data}
          rowKey={(session) => session.id}
          maxHeight="md"
          density="compact"
        />
      ) : null}
    </Card>
  );
}
