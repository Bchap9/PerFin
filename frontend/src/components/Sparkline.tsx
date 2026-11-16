// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A small trend line with a soft area under it, in SVG (`role="img"`). The
 * label must say what the line shows in words (the first and last figure),
 * since the picture has no axis. The path data is computed here; nothing is
 * styled inline (CSP `style-src 'self'`).
 */
import { cx } from "./cx";

export interface SparklineProps {
  points: readonly number[];
  label: string;
  className?: string;
}

const WIDTH = 300;
const HEIGHT = 120;
const PAD = 6;

const fixed = (value: number): string => String(Math.round(value * 10) / 10);

/** The line and area paths for `points`, or null when there is nothing to draw. */
export function sparklinePaths(points: readonly number[]): { line: string; area: string } | null {
  const values = points.filter((point) => Number.isFinite(point));
  if (values.length === 0) {
    return null;
  }
  const series = values.length === 1 ? [values[0] ?? 0, values[0] ?? 0] : values;
  const low = Math.min(...series);
  const high = Math.max(...series);
  const span = high - low;
  const step = WIDTH / (series.length - 1);
  const y = (value: number): number => (span === 0 ? HEIGHT / 2 : PAD + (1 - (value - low) / span) * (HEIGHT - 2 * PAD));
  const coords = series.map((value, index) => `${fixed(index * step)} ${fixed(y(value))}`);
  const line = `M${coords.join(" L")}`;
  const area = `${line} L${fixed(WIDTH)} ${fixed(HEIGHT)} L0 ${fixed(HEIGHT)} Z`;
  return { line, area };
}

export function Sparkline({ points, label, className }: SparklineProps) {
  const paths = sparklinePaths(points);
  return (
    <svg
      className={cx("sparkline", className)}
      viewBox={`0 0 ${String(WIDTH)} ${String(HEIGHT)}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
      focusable="false"
    >
      {paths === null ? null : (
        <>
          <path className="sparkline__area" d={paths.area} />
          <path className="sparkline__line" d={paths.line} vectorEffect="non-scaling-stroke" />
        </>
      )}
    </svg>
  );
}
