// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQuery } from "@tanstack/react-query";

import { Badge, type BadgeTone } from "../../components/Badge";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { getJob, ruleKeys, type Job } from "./api";

const TONES: Record<Job["state"], BadgeTone> = {
  queued: "neutral",
  running: "info",
  succeeded: "success",
  failed: "danger",
  dead: "danger",
};

const LABELS: Record<Job["state"], string> = {
  queued: "Waiting to start",
  running: "Running",
  succeeded: "Finished",
  failed: "Failed",
  dead: "Failed and gave up",
};

export function jobLabel(state: Job["state"]): string {
  // eslint-disable-next-line security/detect-object-injection -- keyed by the closed JobState union
  return LABELS[state];
}

/** Polls `GET /jobs/{id}` every second until the job is finished. */
export function JobProgress({ jobId, onFinished, pollMs = 1000 }: { jobId: string; onFinished?: (job: Job) => void; pollMs?: number }) {
  const query = useQuery({
    queryKey: ruleKeys.job(jobId),
    queryFn: async () => {
      const job = await getJob(jobId);
      if (job.finished) {
        onFinished?.(job);
      }
      return job;
    },
    refetchInterval: (state) => (state.state.data?.finished === true ? false : pollMs),
  });
  if (query.error !== null) {
    return (
      <p role="alert" className="auth-error">
        Could not read the job. {errorMessage(query.error)}
      </p>
    );
  }
  const job = query.data;
  if (job === undefined) {
    return <Spinner label="Starting" />;
  }
  return (
    <p role="status" aria-live="polite">
      <Badge tone={TONES[job.state]}>{LABELS[job.state]}</Badge>
      {job.finished ? null : <> <Spinner size="sm" decorative /> Attempt {job.attempts} of {job.max_attempts}</>}
    </p>
  );
}
