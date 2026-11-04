// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Scrubbed versus original: your own free text next to what an AI client
 * would get, to tune the custom denylist and member aliases before saving.
 * This shows original text, so it is an owner-only view.
 */
import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button } from "../../../components/Button";
import { Card } from "../../../components/Card";
import { Field } from "../../../components/Field";
import { Select } from "../../../components/Select";
import { Spinner } from "../../../components/Spinner";
import { Table, type TableColumn } from "../../../components/Table";
import { authErrorMessage } from "../../auth/errors";
import { previewScrubber, type AiClient, type ScrubberPreview } from "./api";
import { PlaceholderText } from "./PlaceholderText";
import { parseTerms, TextArea } from "./TextArea";

type Sample = ScrubberPreview["samples"][number] & { key: number };

const COLUMNS: readonly TableColumn<Sample>[] = [
  { id: "source", header: "Where", rowHeader: true, cell: (sample) => <code>{sample.source}</code> },
  { id: "original", header: "Original (never sent)", cell: (sample) => <span className="ai-original">{sample.original}</span> },
  {
    id: "scrubbed",
    header: "What the client gets",
    cell: (sample) => (sample.changed ? <PlaceholderText text={sample.scrubbed} /> : <span>{sample.scrubbed}</span>),
  },
];

export function ScrubberSection({ clients }: { clients: readonly AiClient[] }) {
  const [clientId, setClientId] = useState("");
  const [denylist, setDenylist] = useState("");
  const [aliases, setAliases] = useState("");
  const [text, setText] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(true);
  const run = useMutation({ mutationFn: previewScrubber });

  const load = (client: string): void => {
    setClientId(client);
    const found = clients.find((candidate) => candidate.id === client);
    setDenylist(found === undefined ? "" : found.custom_denylist.join("\n"));
    setAliases(found === undefined ? "" : found.member_aliases.join("\n"));
  };

  const onSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    run.mutate({
      ...(clientId === "" ? {} : { client_id: clientId }),
      custom_denylist: parseTerms(denylist),
      member_aliases: parseTerms(aliases),
      ...(text.trim() === "" ? {} : { text }),
      limit: 100,
    });
  };

  const samples: Sample[] = (run.data?.samples ?? []).map((sample, key) => ({ ...sample, key }));
  const shown = onlyChanged ? samples.filter((sample) => sample.changed) : samples;

  return (
    <Card title="Scrubbed versus original">
      <div className="settings-stack">
        <p>
          See your own text next to what a client would receive. Try denylist terms and aliases here before saving them
          on a client.
        </p>
        <form
          className="auth-stack"
          aria-label="Try the scrubber"
          noValidate
          onSubmit={onSubmit}
        >
          <Field label="Start from a client's terms">
            <Select
              name="scrub-client"
              value={clientId}
              options={[{ value: "", label: "No client (household members only)" }, ...clients.map((client) => ({ value: client.id, label: client.name }))]}
              onChange={(event) => {
                load(event.target.value);
              }}
            />
          </Field>
          <Field label="Denylist terms to try" hint="One per line.">
            <TextArea
              name="scrub-denylist"
              rows={3}
              value={denylist}
              onChange={(event) => {
                setDenylist(event.target.value);
              }}
            />
          </Field>
          <Field label="Member aliases to try" hint="One per line.">
            <TextArea
              name="scrub-aliases"
              rows={2}
              value={aliases}
              onChange={(event) => {
                setAliases(event.target.value);
              }}
            />
          </Field>
          <Field label="Your own text" hint="Optional: any text to try, shown only to you.">
            <TextArea
              name="scrub-text"
              rows={2}
              maxLength={4096}
              value={text}
              onChange={(event) => {
                setText(event.target.value);
              }}
            />
          </Field>
          <div className="auth-actions">
            <Button type="submit" variant="primary" loading={run.isPending}>
              Show scrubbed text
            </Button>
          </div>
        </form>
        {run.isPending ? <Spinner label="Scrubbing your text" /> : null}
        {run.isError ? (
          <p className="auth-error" role="alert">
            {authErrorMessage(run.error)}
          </p>
        ) : null}
        {run.isSuccess ? (
          <>
            <label className="ai-inline">
              <input
                type="checkbox"
                checked={onlyChanged}
                onChange={(event) => {
                  setOnlyChanged(event.target.checked);
                }}
              />{" "}
              Only text the scrubber changed
            </label>
            <p role="status" aria-live="polite">
              {String(run.data.changed)} of {String(samples.length)} samples changed.
            </p>
            <Table
              caption="Scrubbed versus original"
              captionHidden
              columns={COLUMNS}
              rows={shown}
              rowKey={(sample) => sample.key}
              empty="Nothing to show."
              density="compact"
              maxHeight="md"
            />
          </>
        ) : null}
      </div>
    </Card>
  );
}
