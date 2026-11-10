// SPDX-License-Identifier: AGPL-3.0-or-later
/** "Find transfers": what a run of the transfer matcher did, in words. */
import type { ToastTone } from "../../components/Toast";
import type { AutoPairResult } from "./api";

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

export interface AutoPairSummary {
  title: string;
  /** Present when some possible transfers were left for the person to link. */
  description?: string;
  tone: ToastTone;
}

/**
 * The matcher links a pair only when it is sure; a transaction with two
 * equally good partners is counted as `ambiguous` and left alone.
 */
export function autoPairSummary(result: AutoPairResult): AutoPairSummary {
  const paired = result.paired.length;
  const title = paired === 0 ? "No new transfers found" : `${plural(paired, "transfer", "transfers")} linked`;
  if (result.ambiguous === 0) {
    return { title, tone: paired === 0 ? "info" : "success" };
  }
  return {
    title,
    description: `${plural(result.ambiguous, "possible transfer has", "possible transfers have")} more than one match. Link ${result.ambiguous === 1 ? "it" : "them"} by hand from the row's Actions menu.`,
    tone: "warning",
  };
}
