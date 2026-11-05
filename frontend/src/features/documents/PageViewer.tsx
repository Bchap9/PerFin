// SPDX-License-Identifier: AGPL-3.0-or-later
import "./documents.css";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Checkbox } from "../../components/Checkbox";
import { Spinner } from "../../components/Spinner";
import { errorMessage } from "../../lib/api/client";
import { docKeys, getRegions, previewUrl, type Region } from "./api";
import { boxStyle, onPage, type PageSize } from "./highlight";

const LABELS: Readonly<Record<Region["label"], string>> = {
  transaction: "Transaction row",
  account_number: "Account number",
  page: "Whole page",
};

export interface PageViewerProps {
  documentId: string;
  /** Highlights to draw; only those on the shown page are positioned. */
  regions?: readonly Region[];
  /** Page to open on (1-based). */
  initialPage?: number;
}

/**
 * A rendered page with highlight boxes laid over it as positioned elements.
 * The image is a cookie-authenticated PNG of the sandbox's render; boxes and
 * word outlines are plain elements placed by percentage (no markup from the
 * document is ever inserted).
 */
export function PageViewer({ documentId, regions = [], initialPage }: PageViewerProps) {
  const first = regions[0]?.page ?? 1;
  const [page, setPage] = useState(initialPage ?? first);
  const [words, setWords] = useState(false);
  const [imageFailed, setImageFailed] = useState(false);
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
  return (
    <div className="page-viewer">
      <div className="ledger-actions">
        <Button
          size="sm"
          disabled={page <= 1}
          onClick={() => {
            setPage(page - 1);
            setImageFailed(false);
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
        <div className="page-viewer__stage">
          <img
            className="page-viewer__image"
            src={previewUrl(documentId, page)}
            alt={`Page ${String(page)} of the document`}
            onError={() => {
              setImageFailed(true);
            }}
          />
          {boxes.map((region, index) => {
            const style = boxStyle(region.bbox, size);
            return style === null ? null : (
              <div
                key={`${String(region.page)}-${String(index)}`}
                className={`page-viewer__box page-viewer__box--${region.label}`}
                data-testid="region-box"
                role="img"
                aria-label={LABELS[region.label]}
                style={style}
              />
            );
          })}
          {words
            ? info.data.words.map((word, index) => {
                const style = boxStyle(word.bbox, size);
                return style === null ? null : (
                  <div key={index} className="page-viewer__word" data-testid="word-box" aria-hidden="true" style={style} />
                );
              })
            : null}
        </div>
      )}
      {info.data.words_truncated && words ? <p className="ledger-muted">Only the first words are outlined.</p> : null}
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
