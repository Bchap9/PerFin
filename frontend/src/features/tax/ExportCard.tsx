// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Accountant export: a zip of the year's documents plus a summary. Owners and
 * admins only; starting it and downloading it (once) each need a recent
 * step-up. The job runs in the worker, so the card polls while it is pending.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { formatDateTime } from "../../lib/dates";
import { StepUpProvider } from "../auth/StepUp";
import { ErrorLine } from "../planning/PlanningPage";
import { useAction } from "../planning/useAction";
import { saveBlob } from "../settings/saveFile";
import { downloadTaxExport, EXPORT_LABEL, EXPORT_TONE, getTaxExport, startTaxExport, taxKeys } from "./api";

function formatSize(bytes: number | null): string {
  if (bytes === null) {
    return "";
  }
  return bytes >= 1_048_576 ? `${(bytes / 1_048_576).toFixed(1)} MB` : `${String(Math.max(1, Math.round(bytes / 1024)))} KB`;
}

export function exportFilename(year: number): string {
  return `perfin-tax-${String(year)}.zip`;
}

function ExportCardBody({ year, canExport, pollMs = 1000 }: { year: number; canExport: boolean; pollMs?: number }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const start = useAction(() => startTaxExport(year), { stepUp: true });
  const query = useQuery({
    queryKey: taxKeys.export(year, jobId ?? "none"),
    enabled: jobId !== null,
    queryFn: () => getTaxExport(year, jobId ?? ""),
    refetchInterval: (state) => (state.state.data?.state === "pending" ? pollMs : false),
  });
  const job = query.data;
  const download = useAction(
    async (id: string) => {
      saveBlob(await downloadTaxExport(year, id), exportFilename(year));
      return true;
    },
    { stepUp: true, success: "Export downloaded", invalidate: [taxKeys.export(year, jobId ?? "none")] },
  );

  const begin = async (): Promise<void> => {
    const done = await start.run();
    if (done !== undefined) {
      setJobId(done.value.job_id);
    }
  };

  return (
    <Card title="Accountant export" headingLevel={2}>
      <div className="planning-form">
        <p>
          A zip with the original documents of {year}, a CSV and a PDF summary of the checklist, form amounts and deductible
          roll-up. It can be downloaded once and expires soon after it is ready.
        </p>
        {canExport ? (
          <div>
            <Button variant="primary" loading={start.busy} disabled={job?.state === "pending"} onClick={() => void begin()}>
              Start export
            </Button>
          </div>
        ) : (
          <p className="muted">Only an owner or admin can export a tax year.</p>
        )}
        <ErrorLine message={start.error} />
        {jobId === null ? null : query.error !== null ? (
          <ErrorLine message={errorMessage(query.error)} />
        ) : job === undefined ? (
          <Spinner label="Reading export status" />
        ) : (
          <div role="status" aria-live="polite" className="planning-form">
            <p>
              <Badge tone={EXPORT_TONE[job.state]}>{EXPORT_LABEL[job.state]}</Badge>
              {job.state === "pending" ? (
                <>
                  {" "}
                  <Spinner size="sm" decorative /> Preparing your export...
                </>
              ) : null}
            </p>
            {job.state === "ready" ? (
              <>
                <p>
                  {formatSize(job.size)}
                  {job.expires_at === null ? "" : `. Expires ${formatDateTime(job.expires_at) ?? job.expires_at}.`}
                </p>
                <div>
                  <Button variant="primary" loading={download.busy} onClick={() => void download.run(job.job_id)}>
                    Download zip
                  </Button>
                </div>
                <ErrorLine message={download.error} />
              </>
            ) : null}
            {job.state === "downloaded" ? <p>Downloaded. The copy on the server has been deleted; start a new export for another.</p> : null}
            {job.state === "expired" ? <p>This export expired. Start a new one.</p> : null}
            {job.state === "failed" ? <p>The export failed. Try again.</p> : null}
          </div>
        )}
      </div>
    </Card>
  );
}

/** The card, with its own step-up dialog (the provider otherwise lives in the settings layout only). */
export function ExportCard(props: { year: number; canExport: boolean; pollMs?: number }) {
  return (
    <StepUpProvider>
      <ExportCardBody {...props} />
    </StepUpProvider>
  );
}
