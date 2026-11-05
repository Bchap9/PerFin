// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Data export and import. Both need a recent second-factor check (step-up):
 * starting an export, downloading it (once) and starting an import.
 */
import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import { Badge, type BadgeTone } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Field } from "../../components/Field";
import { Input } from "../../components/Input";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { formatDateTime } from "../../lib/dates";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { dataKeys, downloadExport, getExport, getImport, startExport, startImport, type ArchiveState } from "./dataApi";
import { exportFilename, saveBlob } from "./saveFile";
import { SettingsPage } from "./SettingsLayout";

const STATE_LABEL: Record<ArchiveState, string> = {
  pending: "Working",
  ready: "Ready to download",
  downloaded: "Downloaded",
  expired: "Expired",
  imported: "Imported",
  failed: "Failed",
};

const STATE_TONE: Record<ArchiveState, BadgeTone> = {
  pending: "info",
  ready: "success",
  downloaded: "neutral",
  expired: "warning",
  imported: "success",
  failed: "danger",
};

function formatSize(bytes: number | null): string {
  if (bytes === null) {
    return "";
  }
  return bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${String(Math.max(1, Math.round(bytes / 1024)))} KB`;
}

export function ExportSection({ pollMs = 1000 }: { pollMs?: number }) {
  const [passphrase, setPassphrase] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [downloaded, setDownloaded] = useState(false);
  const start = useAction(startExport, { stepUp: true });
  const query = useQuery({
    queryKey: dataKeys.export(jobId ?? "none"),
    enabled: jobId !== null,
    queryFn: () => getExport(jobId ?? ""),
    refetchInterval: (state) => (state.state.data?.state === "pending" ? pollMs : false),
  });
  const job = query.data;
  const download = useAction(
    async (id: string, encrypted: boolean) => {
      const blob = await downloadExport(id);
      saveBlob(blob, exportFilename(encrypted));
      return true;
    },
    { stepUp: true, success: "Export downloaded", invalidate: [dataKeys.export(jobId ?? "none")] },
  );

  const begin = async (): Promise<void> => {
    setDownloaded(false);
    const trimmed = passphrase.trim() === "" ? null : passphrase;
    const done = await start.run(trimmed);
    if (done !== undefined) {
      setJobId(done.value.job_id);
    }
  };

  return (
    <Card title="Export your data" headingLevel={2}>
      <div className="planning-form">
        <p>
          Creates one archive with everything in this household: accounts, transactions, budgets, rules and more. Documents are
          included. The archive can be downloaded once, and expires soon after it is ready.
        </p>
        <Field label="Passphrase" hint="Optional. With a passphrase the archive is encrypted; you need it to import the archive again.">
          <Input
            type="password"
            autoComplete="new-password"
            value={passphrase}
            onChange={(event) => {
              setPassphrase(event.target.value);
            }}
          />
        </Field>
        <div>
          <Button variant="primary" loading={start.busy} disabled={job?.state === "pending"} onClick={() => void begin()}>
            Start export
          </Button>
        </div>
        <ErrorLine message={start.error} />
        {jobId === null ? null : query.error !== null ? (
          <ErrorLine message={errorMessage(query.error)} />
        ) : job === undefined ? (
          <Spinner label="Reading export status" />
        ) : (
          <div role="status" aria-live="polite" className="planning-form">
            <p>
              <Badge tone={STATE_TONE[job.state]}>{STATE_LABEL[job.state]}</Badge>
              {job.state === "pending" ? <> <Spinner size="sm" decorative /> Preparing your archive...</> : null}
            </p>
            {job.state === "ready" ? (
              <>
                <p className="muted">
                  {formatSize(job.size)}
                  {job.passphrase_protected ? ", encrypted with your passphrase" : ""}
                  {job.expires_at === null ? "" : `. Download before ${formatDateTime(job.expires_at) ?? job.expires_at}.`}
                </p>
                {job.sha256 === null ? null : <p className="muted">SHA-256: <code>{job.sha256}</code></p>}
                <div>
                  <Button
                    variant="primary"
                    loading={download.busy}
                    onClick={() => {
                      void download.run(job.job_id, job.passphrase_protected).then((done) => {
                        if (done !== undefined) {
                          setDownloaded(true);
                        }
                      });
                    }}
                  >
                    Download archive
                  </Button>
                </div>
              </>
            ) : null}
            {job.state === "failed" ? <ErrorLine message={job.error ?? "The export failed."} /> : null}
            {job.state === "expired" ? <p>This export expired. Start a new one.</p> : null}
            {downloaded || job.state === "downloaded" ? <p>The archive was downloaded. It cannot be downloaded again.</p> : null}
          </div>
        )}
        <ErrorLine message={download.error} />
      </div>
    </Card>
  );
}

export function ImportSection({ pollMs = 1000 }: { pollMs?: number }) {
  const fileId = useId();
  const [file, setFile] = useState<File | null>(null);
  const [passphrase, setPassphrase] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const start = useAction(startImport, { stepUp: true });
  const query = useQuery({
    queryKey: dataKeys.import(jobId ?? "none"),
    enabled: jobId !== null,
    queryFn: () => getImport(jobId ?? ""),
    refetchInterval: (state) => (state.state.data?.state === "pending" ? pollMs : false),
  });
  const job = query.data;

  return (
    <Card title="Import an archive" headingLevel={2}>
      <div className="planning-form">
        <p>Restore an archive made by PerFin's export. Existing records are kept; the archive's records are added or merged.</p>
        <Field label="Archive file" id={fileId}>
          <Input
            type="file"
            accept=".zip,.pfarch,application/zip,application/octet-stream"
            onChange={(event) => {
              setFile(event.target.files?.item(0) ?? null);
            }}
          />
        </Field>
        <Field label="Archive passphrase" hint="Only if the archive was encrypted.">
          <Input type="password" autoComplete="off" value={passphrase} onChange={(event) => { setPassphrase(event.target.value); }} />
        </Field>
        <div>
          <Button
            variant="primary"
            loading={start.busy}
            disabled={file === null || job?.state === "pending"}
            onClick={() => {
              if (file !== null) {
                void start.run(file, passphrase === "" ? null : passphrase).then((done) => {
                  if (done !== undefined) {
                    setJobId(done.value.job_id);
                  }
                });
              }
            }}
          >
            Import archive
          </Button>
        </div>
        <ErrorLine message={start.error} />
        {jobId === null ? null : query.error !== null ? (
          <ErrorLine message={errorMessage(query.error)} />
        ) : job === undefined ? (
          <Spinner label="Reading import status" />
        ) : (
          <div role="status" aria-live="polite" className="planning-form">
            <p>
              <Badge tone={STATE_TONE[job.state]}>{STATE_LABEL[job.state]}</Badge>
              {job.state === "pending" ? <> <Spinner size="sm" decorative /> Importing...</> : null}
            </p>
            {job.state === "failed" ? <ErrorLine message={job.error_detail ?? job.error ?? "The import failed."} /> : null}
            {job.state === "imported" ? (
              <ul className="planning-list" aria-label="Import summary">
                {Object.entries(job.summary)
                  .filter(([, value]) => typeof value === "number" || typeof value === "string")
                  .map(([key, value]) => (
                    <li key={key}>
                      {key.replaceAll("_", " ")}: {String(value)}
                    </li>
                  ))}
              </ul>
            ) : null}
          </div>
        )}
      </div>
    </Card>
  );
}

export function DataPage() {
  return (
    <SettingsPage title="Data export and import">
      <ExportSection />
      <ImportSection />
    </SettingsPage>
  );
}
