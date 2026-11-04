// SPDX-License-Identifier: AGPL-3.0-or-later
/** AI clients: list, create (wizard, token shown once), preview and revoke. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge, type BadgeTone } from "../../../components/Badge";
import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Dialog } from "../../../components/Dialog";
import { Spinner } from "../../../components/Spinner";
import { Table, type TableColumn } from "../../../components/Table";
import { useToast } from "../../../components/Toast";
import { sessionQueryOptions } from "../../../lib/session";
import { authErrorMessage } from "../../auth/errors";
import { ShowOnceDialog } from "../../auth/ShowOnceDialog";
import { isStepUpCancelled, useStepUp } from "../../auth/StepUp";
import { when } from "../SettingsLayout";
import { aiKeys, listClients, revokeClient, type AiClient, type AiClientCreated } from "./api";
import { ClientWizard } from "./ClientWizard";
import { ClientPreview } from "./ExposurePreview";
import { SAVE_TOKEN_COMMAND, setupSnippet } from "./setup";

const TONES: Readonly<Record<AiClient["status"], BadgeTone>> = { active: "success", expired: "neutral", revoked: "danger" };
const LABELS: Readonly<Record<AiClient["status"], string>> = { active: "Active", expired: "Expired", revoked: "Revoked" };

function optionsText(client: AiClient): string {
  const parts: string[] = [client.mode === "detail" ? "Detail" : "Aggregates"];
  if (client.rounding !== null) {
    parts.push(`rounded to ${String(client.rounding)}`);
  }
  if (client.hide_nicknames) {
    parts.push("nicknames hidden");
  }
  if (client.hide_merchant_location) {
    parts.push("locations hidden");
  }
  if (client.date_range_cap !== null) {
    parts.push(`${String(client.date_range_cap)} day limit`);
  }
  if (client.custom_denylist.length > 0) {
    parts.push(`${String(client.custom_denylist.length)} denylist entries`);
  }
  return parts.join(", ");
}

export function ClientsSection() {
  const queryClient = useQueryClient();
  const { notify } = useToast();
  const runWithStepUp = useStepUp();
  const clients = useQuery({ queryKey: aiKeys.clients, queryFn: listClients });
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<AiClientCreated | null>(null);
  const [confirm, setConfirm] = useState<AiClient | null>(null);
  const [previewing, setPreviewing] = useState<AiClient | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: aiKeys.clients });
    await queryClient.invalidateQueries({ queryKey: aiKeys.access });
    await queryClient.invalidateQueries({ queryKey: sessionQueryOptions().queryKey });
  };

  const revoke = async (client: AiClient): Promise<void> => {
    setConfirm(null);
    setBusyId(client.id);
    try {
      await runWithStepUp(() => revokeClient(client.id));
      notify({ tone: "success", title: `${client.name} revoked`, description: "Its token stopped working now." });
    } catch (caught) {
      if (!isStepUpCancelled(caught)) {
        notify({ tone: "error", title: "Could not revoke the client", description: authErrorMessage(caught) });
      }
    } finally {
      setBusyId(null);
      await refresh();
    }
  };

  const columns: readonly TableColumn<AiClient>[] = [
    { id: "name", header: "Name", rowHeader: true, cell: (client) => client.name },
    { id: "what", header: "Shares", cell: (client) => optionsText(client) },
    { id: "transport", header: "Transport", cell: (client) => client.transport },
    { id: "status", header: "Status", cell: (client) => <Badge tone={TONES[client.status]}>{LABELS[client.status]}</Badge> },
    { id: "expires", header: "Expires", cell: (client) => when(client.expires_at) },
    { id: "used", header: "Last used", cell: (client) => when(client.last_used_at) },
    {
      id: "actions",
      header: <span className="visually-hidden">Actions</span>,
      align: "end",
      cell: (client) => (
        <span className="ai-row-actions">
          <Button
            size="sm"
            aria-label={`Preview ${client.name}`}
            onClick={() => {
              setPreviewing(client);
            }}
          >
            Preview
          </Button>
          {client.status === "active" ? (
            <Button
              size="sm"
              variant="danger"
              loading={busyId === client.id}
              aria-label={`Revoke ${client.name}`}
              onClick={() => {
                setConfirm(client);
              }}
            >
              Revoke
            </Button>
          ) : null}
        </span>
      ),
    },
  ];

  return (
    <Card
      title="AI clients"
      actions={
        creating ? null : (
          <Button
            variant="primary"
            onClick={() => {
              setCreating(true);
            }}
          >
            New client
          </Button>
        )
      }
    >
      <div className="settings-stack">
        <p>
          A client is one assistant that may read your financial facts through PerFin&apos;s MCP server. It never sees
          names, addresses, account numbers or documents.
        </p>
        {creating ? (
          <ClientWizard
            onCancel={() => {
              setCreating(false);
            }}
            onCreated={(client) => {
              setCreating(false);
              setCreated(client);
              void refresh();
            }}
          />
        ) : null}
        {clients.isPending ? <Spinner label="Loading AI clients" /> : null}
        {clients.isError ? (
          <p className="auth-error" role="alert">
            {authErrorMessage(clients.error)}
          </p>
        ) : null}
        {clients.isSuccess ? (
          <Table
            caption="AI clients"
            captionHidden
            columns={columns}
            rows={clients.data}
            rowKey={(client) => client.id}
            empty="No AI clients. Nothing is shared."
            density="compact"
          />
        ) : null}
      </div>

      <ShowOnceDialog
        open={created !== null}
        title="Your new AI client"
        description="Copy the token now. PerFin stores only a hash and cannot show it again."
        lines={created === null ? [] : [created.token]}
        label="token"
        onDone={() => {
          setCreated(null);
        }}
      >
        {created === null ? null : (
          <div className="settings-stack">
            <p>
              Store it in a file only you can read, then add the server to your assistant (see docs/MCP_CLIENTS.md for
              Claude Desktop, Claude Code and others):
            </p>
            <pre className="ai-snippet" aria-label="Save the token">
              {SAVE_TOKEN_COMMAND}
            </pre>
            <pre className="ai-snippet" aria-label="MCP setup snippet" tabIndex={0}>
              {setupSnippet(created.transport, window.location.origin)}
            </pre>
          </div>
        )}
      </ShowOnceDialog>

      <Dialog
        open={confirm !== null}
        onClose={() => {
          setConfirm(null);
        }}
        title={confirm === null ? "Revoke" : `Revoke ${confirm.name}?`}
        description="Its token stops working immediately. This cannot be undone."
        footer={
          <>
            <Button
              onClick={() => {
                setConfirm(null);
              }}
            >
              Keep it
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (confirm !== null) {
                  void revoke(confirm);
                }
              }}
            >
              Revoke
            </Button>
          </>
        }
      >
        <p>You will be asked for your second factor.</p>
      </Dialog>

      <Dialog
        open={previewing !== null}
        onClose={() => {
          setPreviewing(null);
        }}
        size="lg"
        title={previewing === null ? "Preview" : `What ${previewing.name} sees`}
        description="Every tool on your real data with this client's saved options."
      >
        {previewing === null ? null : <ClientPreview clientId={previewing.id} />}
      </Dialog>
    </Card>
  );
}
