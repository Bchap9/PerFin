// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The audit of AI calls: what each call asked for and exactly what was sent
 * back. The search box finds any string in the stored responses, to confirm
 * that something never left.
 */
import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Field } from "../../../components/Field";
import { Input } from "../../../components/Input";
import { Select } from "../../../components/Select";
import { Spinner } from "../../../components/Spinner";
import { authErrorMessage } from "../../auth/errors";
import { when } from "../SettingsLayout";
import { aiKeys, listCalls, type AiCall, type AiClient } from "./api";
import { JsonWithPlaceholders } from "./PlaceholderText";

function CallRow({ call, clientName }: { call: AiCall; clientName: string }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="ai-call">
      <details
        onToggle={(event) => {
          setOpen(event.currentTarget.open);
        }}
      >
        <summary>
          <span>{when(call.created_at)}</span> <strong>{clientName}</strong> <code>{call.tool}</code>{" "}
          <span className="auth-muted">
            status {String(call.status)}, {String(call.bytes)} bytes
          </span>
        </summary>
        {open ? (
          <>
            <JsonWithPlaceholders value={call.params} label={`Parameters of the ${call.tool} call`} />
            <JsonWithPlaceholders value={call.response} label={`Response of the ${call.tool} call`} />
          </>
        ) : null}
      </details>
    </li>
  );
}

export function CallsSection({ clients }: { clients: readonly AiClient[] }) {
  const [clientId, setClientId] = useState("");
  const [typed, setTyped] = useState("");
  const [contains, setContains] = useState("");
  const query = useQuery({
    queryKey: aiKeys.calls(clientId, contains),
    queryFn: () => listCalls(clientId, contains),
    staleTime: 0,
  });
  const names = new Map(clients.map((client) => [client.id, client.name]));

  const onSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setContains(typed.trim());
  };

  const searching = contains !== "";
  return (
    <Card title="Calls">
      <div className="settings-stack">
        <p>
          Every call an AI client made, with the answer exactly as it was sent. Search for a word, a name or a number
          to confirm it never left.
        </p>
        <form className="ai-search" role="search" aria-label="Search AI calls" onSubmit={onSubmit}>
          <Field label="Search the responses">
            <Input
              name="calls-contains"
              maxLength={200}
              value={typed}
              onChange={(event) => {
                setTyped(event.target.value);
              }}
            />
          </Field>
          <Field label="Client">
            <Select
              name="calls-client"
              value={clientId}
              options={[{ value: "", label: "All clients" }, ...clients.map((client) => ({ value: client.id, label: client.name }))]}
              onChange={(event) => {
                setClientId(event.target.value);
              }}
            />
          </Field>
          <Button type="submit" variant="primary">
            Search
          </Button>
        </form>
        {query.isPending ? <Spinner label="Loading calls" /> : null}
        {query.isError ? (
          <p className="auth-error" role="alert">
            {authErrorMessage(query.error)}
          </p>
        ) : null}
        {query.isSuccess ? (
          <>
            <p role="status" aria-live="polite">
              {searching
                ? query.data.length === 0
                  ? `No recorded response contains "${contains}". It was never sent.`
                  : `${String(query.data.length)} ${query.data.length === 1 ? "call contains" : "calls contain"} "${contains}".`
                : query.data.length === 0
                  ? "No AI calls yet."
                  : `${String(query.data.length)} most recent calls.`}
            </p>
            <ul className="ai-calls">
              {query.data.map((call) => (
                <CallRow key={call.id} call={call} clientName={names.get(call.client_id) ?? "Removed client"} />
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </Card>
  );
}
