// SPDX-License-Identifier: AGPL-3.0-or-later
import type { ReactNode } from "react";

import { splitPlaceholders } from "./placeholders";

/** Text with its scrubber placeholders in styled spans (text nodes only; never HTML). */
export function PlaceholderText({ text }: { text: string }) {
  return (
    <>
      {splitPlaceholders(text).map((segment, index) =>
        segment.kind === "placeholder" ? (
          <mark key={index} className="ai-placeholder" data-placeholder={segment.text}>
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  );
}

const MAX_DEPTH = 12;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function renderValue(value: unknown, indent: number, depth: number): ReactNode {
  const pad = "  ".repeat(indent);
  const inner = "  ".repeat(indent + 1);
  if (typeof value === "string") {
    return (
      <>
        &quot;
        <PlaceholderText text={value} />
        &quot;
      </>
    );
  }
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (depth >= MAX_DEPTH) {
    return "…";
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    return (
      <>
        {"[\n"}
        {value.map((item: unknown, index) => (
          <span key={index}>
            {inner}
            {renderValue(item, indent + 1, depth + 1)}
            {index < value.length - 1 ? ",\n" : "\n"}
          </span>
        ))}
        {pad}]
      </>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return "{}";
    }
    return (
      <>
        {"{\n"}
        {entries.map(([key, item], index) => (
          <span key={key}>
            {inner}
            <PlaceholderText text={JSON.stringify(key)} />
            {": "}
            {renderValue(item, indent + 1, depth + 1)}
            {index < entries.length - 1 ? ",\n" : "\n"}
          </span>
        ))}
        {pad}
        {"}"}
      </>
    );
  }
  return "";
}

/** A JSON value, pretty-printed, with placeholders in strings highlighted. */
export function JsonWithPlaceholders({ value, label }: { value: unknown; label: string }) {
  return (
    <pre className="ai-json" aria-label={label} tabIndex={0}>
      {renderValue(value, 0, 0)}
    </pre>
  );
}
