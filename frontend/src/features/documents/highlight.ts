// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Positioning of region highlights over a rendered page.
 *
 * Boxes arrive in page units (origin top left, `[x0, y0, x1, y1]`); the
 * preview image is a scaled copy of the page, so boxes are placed as
 * percentages of the page size and stay correct at any rendered size.
 */
export interface PageSize {
  width: number;
  height: number;
}

export interface BoxStyle {
  left: string;
  top: string;
  width: string;
  height: string;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

function percent(value: number): string {
  return `${String(Math.round(value * 1000) / 1000)}%`;
}

/** Percent style of a box on a page; `null` for a malformed, empty or off-page box. */
export function boxStyle(bbox: readonly number[], page: PageSize): BoxStyle | null {
  const [x0, y0, x1, y1] = bbox;
  if (
    bbox.length !== 4 ||
    x0 === undefined ||
    y0 === undefined ||
    x1 === undefined ||
    y1 === undefined ||
    ![x0, y0, x1, y1, page.width, page.height].every(Number.isFinite) ||
    page.width <= 0 ||
    page.height <= 0
  ) {
    return null;
  }
  const left = clamp((Math.min(x0, x1) / page.width) * 100, 0, 100);
  const right = clamp((Math.max(x0, x1) / page.width) * 100, 0, 100);
  const top = clamp((Math.min(y0, y1) / page.height) * 100, 0, 100);
  const bottom = clamp((Math.max(y0, y1) / page.height) * 100, 0, 100);
  if (right - left <= 0 || bottom - top <= 0) {
    return null;
  }
  return { left: percent(left), top: percent(top), width: percent(right - left), height: percent(bottom - top) };
}

export interface PagedBox {
  page: number;
  bbox: readonly number[];
}

export function onPage<T extends PagedBox>(boxes: readonly T[], page: number): T[] {
  return boxes.filter((box) => box.page === page);
}

/** Pages that carry at least one highlight, ascending. */
export function pagesWithBoxes(boxes: readonly PagedBox[]): number[] {
  return [...new Set(boxes.map((box) => box.page))].sort((a, b) => a - b);
}

/** Whether a word box lies (by its centre) inside a region box; used to mark the words a region covers. */
export function wordInside(word: readonly number[], region: readonly number[]): boolean {
  const [wx0, wy0, wx1, wy1] = word;
  const [rx0, ry0, rx1, ry1] = region;
  if ([wx0, wy0, wx1, wy1, rx0, ry0, rx1, ry1].some((value) => value === undefined)) {
    return false;
  }
  const cx = ((wx0 ?? 0) + (wx1 ?? 0)) / 2;
  const cy = ((wy0 ?? 0) + (wy1 ?? 0)) / 2;
  return cx >= (rx0 ?? 0) && cx <= (rx1 ?? 0) && cy >= (ry0 ?? 0) && cy <= (ry1 ?? 0);
}
