// SPDX-License-Identifier: AGPL-3.0-or-later
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { startEventStream } from "../lib/sse";

/** Keep the event stream open while `enabled` (i.e. while signed in). */
export function useLiveUpdates(enabled: boolean): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") {
      return undefined;
    }
    const stream = startEventStream({ queryClient });
    return () => {
      stream.close();
    };
  }, [enabled, queryClient]);
}
