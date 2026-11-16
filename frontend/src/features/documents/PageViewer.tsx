// SPDX-License-Identifier: AGPL-3.0-or-later
import "./documents.css";

import { useQuery } from "@tanstack/react-query";
import { useRef, useState, type PointerEvent } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { boxAround, pageLines, wordAt, wordsIn, type PageWord } from "../review/teach";
import { docKeys, getRegions, previewUrl, type Region } from "./api";
import { boxStyle, onPage, type PageSize } from "./highlight";

const LABELS: Readonly<Record<Region["label"], string>> = {
  transaction: "Transaction row",
  account_number: "Account number",
  page: "Whole page",
  skipped_line: "Skipped line",
};

/** A box the person selected (drawn over the page with its label). */
export interface PageSelection {
  page: number;
  bbox: readonly number[];
  label: string;
}

type Box = [number, number, number, number];
/** Below this many page units a drag is a click on a word. */
const CLICK_SLOP = 3;

/** A box in page units, as the attributes of an SVG `rect` on the overlay. */
interface PageRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/**
 * The rect of a box on the page, clamped to the page with its corners in
 * order; `null` for a malformed, empty or off-page box (as `boxStyle`).
 */
function pageRect(bbox: readonly number[], size: PageSize): PageRect | null {
  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = bbox;
  if (boxStyle(bbox, size) === null) {
    return null;
  }
  const left = clamp(Math.min(x0, x1), 0, size.width);
  const top = clamp(Math.min(y0, y1), 0, size.height);
  const right = clamp(Math.max(x0, x1), 0, size.width);
  const bottom = clamp(Math.max(y0, y1), 0, size.height);
  return { x: round(left), y: round(top), width: round(right - left), height: round(bottom - top) };
}

/** The six numbers of a screen matrix (`SVGGraphicsElement.getScreenCTM()`). */
type ScreenMatrix = Pick<DOMMatrix, "a" | "b" | "c" | "d" | "e" | "f">;

/** A screen point in page units, through the inverse of the overlay's screen matrix. */
function fromScreen(matrix: ScreenMatrix, clientX: number, clientY: number): [number, number] | null {
  const { a, b, c, d, e, f } = matrix;
  const det = a * d - b * c;
  if (!Number.isFinite(det) || det === 0) {
    return null;
  }
  const dx = clientX - e;
  const dy = clientY - f;
  return [(d * dx - c * dy) / det, (a * dy - b * dx) / det];
}

export interface PageViewerProps {
  documentId: string;
  /** Highlights to draw; only those on the shown page are positioned. */
  regions?: readonly Region[];
  /** Page to open on (1-based). */
  initialPage?: number;
  /**
   * Selection mode ("Teach PerFin"): a click picks a word, a drag a box, and
   * a list of the page's lines does the same from the keyboard.
   */
  onSelect?: (page: number, bbox: Box, words: readonly PageWord[]) => void;
  /** Selected boxes to draw (only those on the shown page). */
  selections?: readonly PageSelection[];
}

/**
 * A rendered page with highlight boxes laid over it on an SVG overlay. The
 * image is a cookie-authenticated PNG of the sandbox's render; the overlay's
 * viewBox is the page in page units, and boxes and word outlines are `rect`
 * attributes on it (no style attribute, no markup from the document).
 */
export function PageViewer({
  documentId,
  regions = [],
  initialPage,
  onSelect,
  selections = [],
}: PageViewerProps) {
  const first = regions[0]?.page ?? 1;
  const [page, setPage] = useState(initialPage ?? first);
  const [words, setWords] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
  /** The line chosen in the keyboard picker; added only with "Add this line". */
  const [chosenLine, setChosenLine] = useState("");
  const [drag, setDrag] = useState<{ start: [number, number]; end: [number, number] } | null>(
    null,
  );
  const overlay = useRef<SVGSVGElement>(null);
  const info = useQuery({
    queryKey: docKeys.regions(documentId, page),
    queryFn: () => getRegions(documentId, page),
  });
  if (info.isPending) {
    return <Spinner label="Loading page" />;
  }
  if (info.isError) {
    return (
      <p role="alert" className="ledger-error">
        {errorMessage(info.error)}
      </p>
    );
  }
  const size: PageSize = { width: info.data.width, height: info.data.height };
  const count = Math.max(1, info.data.page_count);
  const boxes = onPage(regions, page);
  const pageWords: readonly PageWord[] = info.data.words;
  const selecting = onSelect !== undefined;
  const lines = selecting ? pageLines(pageWords) : [];

  // The overlay's viewBox is the page, so its screen matrix maps page units
  // to the screen at any rendered size; its inverse maps the pointer back.
  const toPage = (event: PointerEvent<HTMLDivElement>): [number, number] | null => {
    const svg = overlay.current;
    const matrix = svg !== null && typeof svg.getScreenCTM === "function" ? svg.getScreenCTM() : null;
    return matrix === null ? null : fromScreen(matrix, event.clientX, event.clientY);
  };
  const finish = (start: [number, number], end: [number, number]): void => {
    if (onSelect === undefined) {
      return;
    }
    const box: Box = [
      Math.min(start[0], end[0]),
      Math.min(start[1], end[1]),
      Math.max(start[0], end[0]),
      Math.max(start[1], end[1]),
    ];
    if (box[2] - box[0] < CLICK_SLOP && box[3] - box[1] < CLICK_SLOP) {
      const word = wordAt(pageWords, end[0], end[1]);
      const around = word === null ? null : boxAround([word]);
      if (around !== null) {
        onSelect(page, around, pageWords);
      }
      return;
    }
    if (wordsIn(pageWords, box).length > 0) {
      onSelect(page, box, pageWords);
    }
  };
  const dragRect =
    drag === null
      ? null
      : pageRect(
          [
            Math.min(drag.start[0], drag.end[0]),
            Math.min(drag.start[1], drag.end[1]),
            Math.max(drag.start[0], drag.end[0]),
            Math.max(drag.start[1], drag.end[1]),
          ],
          size,
        );
  return (
    <div className="page-viewer">
      <div className="ledger-actions">
        <Button
          size="sm"
          disabled={page <= 1}
          onClick={() => {
            setPage(page - 1);
            setImageFailed(false);
            setChosenLine("");
          }}
        >
          Previous page
        </Button>
        <span>
          Page {page} of {count}
        </span>
        <Button
          size="sm"
          disabled={page >= count}
          onClick={() => {
            setPage(page + 1);
            setImageFailed(false);
            setChosenLine("");
          }}
        >
          Next page
        </Button>
        {info.data.text_included ? (
          <Checkbox
            label="Outline recognised words"
            checked={words}
            onChange={(event) => {
              setWords(event.target.checked);
            }}
          />
        ) : null}
      </div>
      {imageFailed ? (
        <p role="alert" className="ledger-error">
          The page preview is not available.
        </p>
      ) : (
        <div
          className={selecting ? "page-viewer__stage page-viewer__stage--select" : "page-viewer__stage"}
          onPointerDown={
            selecting
              ? (event) => {
                  const at = toPage(event);
                  if (at !== null) {
                    event.preventDefault();
                    setDrag({ start: at, end: at });
                  }
                }
              : undefined
          }
          onPointerMove={
            selecting && drag !== null
              ? (event) => {
                  const at = toPage(event);
                  if (at !== null) {
                    setDrag({ start: drag.start, end: at });
                  }
                }
              : undefined
          }
          onPointerUp={
            selecting
              ? (event) => {
                  const at = toPage(event);
                  if (drag !== null && at !== null) {
                    finish(drag.start, at);
                  }
                  setDrag(null);
                }
              : undefined
          }
        >
          <img
            draggable={false}
            className="page-viewer__image"
            src={previewUrl(documentId, page)}
            alt={`Page ${String(page)} of the document`}
            onError={() => {
              setImageFailed(true);
            }}
          />
          <svg
            ref={overlay}
            className="page-viewer__overlay"
            data-testid="page-overlay"
            viewBox={`0 0 ${String(size.width)} ${String(size.height)}`}
            preserveAspectRatio="none"
            focusable="false"
          >
            {boxes.map((region, index) => {
              const rect = pageRect(region.bbox, size);
              return rect === null ? null : (
                <rect
                  key={`${String(region.page)}-${String(index)}`}
                  className={`page-viewer__box page-viewer__box--${region.label}`}
                  data-testid="region-box"
                  role="img"
                  aria-label={LABELS[region.label]}
                  {...rect}
                />
              );
            })}
            {onPage(selections, page).map((selection, index) => {
              const rect = pageRect(selection.bbox, size);
              return rect === null ? null : (
                <rect
                  key={`selection-${String(index)}`}
                  className="page-viewer__box page-viewer__box--selection"
                  data-testid="selection-box"
                  role="img"
                  aria-label={`Selected: ${selection.label}`}
                  {...rect}
                />
              );
            })}
            {dragRect === null ? null : (
              <rect className="page-viewer__box page-viewer__box--drag" aria-hidden="true" {...dragRect} />
            )}
            {words
              ? info.data.words.map((word, index) => {
                  const rect = pageRect(word.bbox, size);
                  return rect === null ? null : (
                    <rect key={index} className="page-viewer__word" data-testid="word-box" aria-hidden="true" {...rect} />
                  );
                })
              : null}
          </svg>
        </div>
      )}
      {info.data.words_truncated && words ? <p className="ledger-muted">Only the first words are outlined.</p> : null}
      {selecting ? (
        info.data.text_included ? (
          <div className="ledger-stack">
            <Field
              label="Choose a line"
              hint="Click a word or drag a box on the page, or pick a whole line here and add it."
            >
              <Select
                placeholder="Pick a line of this page"
                value={chosenLine}
                options={lines.map((line) => ({
                  value: String(line.line),
                  label: line.text.length > 90 ? `${line.text.slice(0, 90)}…` : line.text,
                }))}
                onChange={(event) => {
                  // Choosing only moves the choice (arrow keys on a closed
                  // select fire change); the button adds it (WCAG 3.2.2).
                  setChosenLine(event.target.value);
                }}
              />
            </Field>
            <div className="ledger-actions">
              <Button
                size="sm"
                disabled={chosenLine === ""}
                onClick={() => {
                  const found = lines.find((line) => String(line.line) === chosenLine);
                  if (found !== undefined) {
                    onSelect(page, found.bbox, pageWords);
                    setChosenLine("");
                  }
                }}
              >
                Add this line
              </Button>
            </div>
          </div>
        ) : (
          <p className="ledger-muted">Your role cannot see the words of this page.</p>
        )
      ) : null}
      {boxes.length > 0 ? (
        <ul className="ledger-muted" aria-label="Highlighted on this page">
          {boxes.map((region, index) => (
            <li key={index}>
              {LABELS[region.label]}
              {region.fields !== undefined && region.fields.length > 0 ? `: ${region.fields.join(", ")}` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
