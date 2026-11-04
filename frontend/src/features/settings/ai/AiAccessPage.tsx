// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Settings > AI access (docs/AI_ACCESS.md, SECURITY.md E3/M2/M5): the
 * household owner manages the assistants that may read financial facts
 * through the MCP server, previews exactly what leaves, audits every call and
 * tunes the scrubber. Other members see why they cannot.
 */
import { useQuery } from "@tanstack/react-query";

import { Card } from "../../../components/Card";
import { Spinner } from "../../../components/Spinner";
import { Tabs } from "../../../components/Tabs";
import { useAuthSession } from "../../auth/session";
import { SettingsPage } from "../SettingsLayout";
import { aiKeys, listClients } from "./api";
import { CallsSection } from "./CallsSection";
import { ClientsSection } from "./ClientsSection";
import "./ai.css";
import { ScrubberSection } from "./ScrubberSection";
import { SwitchesSection } from "./SwitchesSection";

function OwnerContent() {
  const clients = useQuery({ queryKey: aiKeys.clients, queryFn: listClients });
  const list = clients.data ?? [];
  return (
    <>
      <SwitchesSection />
      <Tabs
        label="AI access"
        tabs={[
          { id: "clients", label: "Clients", content: <ClientsSection /> },
          { id: "calls", label: "Calls", content: <CallsSection clients={list} /> },
          { id: "scrubber", label: "Scrubbed vs original", content: <ScrubberSection clients={list} /> },
        ]}
      />
    </>
  );
}

export function AiAccessPage() {
  const { session, query } = useAuthSession();
  return (
    <SettingsPage title="AI access">
      {query.isPending ? <Spinner label="Loading" /> : null}
      {session?.household?.role === "owner" ? (
        <OwnerContent />
      ) : session === null ? null : (
        <Card>
          <p>
            Only the household owner manages AI access. Clients can read financial facts only: never names, addresses,
            account numbers or documents.
          </p>
        </Card>
      )}
    </SettingsPage>
  );
}
