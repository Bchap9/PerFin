// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * How far a value is toward a maximum (a budget, a goal), drawn in SVG so
 * the width is an attribute, not a style (CSP `style-src 'self'`). The value
 * rect is clamped to the track; `over` turns it to the danger colour, and the
 * label should say "over" in words too.
 */
import { cx } from "./cx";

export interface ProgressBarProps {
  value: number;
  max: number;
  label: string;
  over?: boolean;
  className?: string;
}

/** Percent of the track to fill, 0 to 100; 0 for a zero, negative or broken input. */
export function progressWidth(value: number, max: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(max) || max <= 0 || value <= 0) {
    return 0;
  }
  return Math.min(100, Math.round((value / max) * 1000) / 10);
}

export function ProgressBar({ value, max, label, over = false, className }: ProgressBarProps) {
  const safeMax = Number.isFinite(max) && max > 0 ? max : 0;
  const safeValue = Number.isFinite(value) ? value : 0;
  return (
    <svg
      className={cx("progress", over && "progress--over", className)}
      viewBox="0 0 100 8"
      preserveAspectRatio="none"
      role="progressbar"
      aria-label={label}
      aria-valuenow={safeValue}
      aria-valuemin={0}
      aria-valuemax={safeMax}
      focusable="false"
    >
      <rect className="progress__track" x="0" y="0" width="100" height="8" rx="4" />
      <rect className="progress__value" x="0" y="0" width={progressWidth(value, max)} height="8" rx="4" />
    </svg>
  );
}
