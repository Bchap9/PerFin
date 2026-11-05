// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A write with busy and error state: runs `action` (through step-up when
 * asked), invalidates query families, toasts and reports completion. A
 * cancelled step-up is silent.
 */
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { useToast } from "../../components/Toast";
import { errorMessage } from "../../lib/api/client";
import { isStepUpCancelled, useStepUp } from "../auth/StepUp";

export interface ActionOptions<R> {
  invalidate?: readonly QueryKey[];
  success?: string | ((result: R) => string);
  stepUp?: boolean;
}

export interface ActionState<A extends unknown[], R> {
  /** Resolves to `{ value }` on success and `undefined` when the action failed or was cancelled. */
  run: (...args: A) => Promise<{ value: R } | undefined>;
  busy: boolean;
  error: string | null;
  clearError: () => void;
}

export function useAction<A extends unknown[], R>(
  action: (...args: A) => Promise<R>,
  options: ActionOptions<R> = {},
): ActionState<A, R> {
  const queryClient = useQueryClient();
  const toast = useToast();
  const runWithStepUp = useStepUp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef({ action, options });
  useEffect(() => {
    latest.current = { action, options };
  });

  const run = useCallback(
    async (...args: A): Promise<{ value: R } | undefined> => {
      const { action: current, options: opts } = latest.current;
      setBusy(true);
      setError(null);
      try {
        const result = await (opts.stepUp === true ? runWithStepUp(() => current(...args)) : current(...args));
        await Promise.all((opts.invalidate ?? []).map((queryKey) => queryClient.invalidateQueries({ queryKey })));
        if (opts.success !== undefined) {
          toast.notify({ tone: "success", title: typeof opts.success === "function" ? opts.success(result) : opts.success });
        }
        return { value: result };
      } catch (caught) {
        if (!isStepUpCancelled(caught)) {
          setError(errorMessage(caught));
        }
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [queryClient, toast, runWithStepUp],
  );

  return { run, busy, error, clearError: () => { setError(null); } };
}
