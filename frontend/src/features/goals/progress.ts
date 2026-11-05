// SPDX-License-Identifier: AGPL-3.0-or-later
import { formatDate } from "../../lib/dates";
import type { Goal } from "./api";

/** The goal's percent as a bar width (0 to 100); the server sends a decimal string. */
export function goalBarWidth(goal: Goal): number {
  const value = Number.parseFloat(goal.progress.percent);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

/** One sentence on where the goal is heading. */
export function projectionText(goal: Goal): string {
  const { progress } = goal;
  if (goal.achieved_at !== null) {
    return "Goal reached.";
  }
  if (progress.projected_completion !== null) {
    const date = formatDate(progress.projected_completion) ?? progress.projected_completion;
    if (progress.on_track === false && goal.target_date !== null) {
      return `At the current pace you would reach it on ${date}, after the ${formatDate(goal.target_date) ?? goal.target_date} target.`;
    }
    return `At the current pace you would reach it on ${date}.`;
  }
  return "Not enough history to project a completion date yet.";
}
