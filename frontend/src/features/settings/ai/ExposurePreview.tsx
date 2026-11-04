// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * The exposure preview: every AI tool run on the household's real data with
 * the chosen options, exactly as the AI view would send it. Placeholders the
 * scrubber inserted are highlighted. Nothing is stored or sent.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { Badge } from "../../../components/Badge";
import { Spinner } from "../../../components/Spinner";
import { queryRoots } from "../../../lib/queryKeys";
import { authErrorMessage } from "../../auth/errors";
import { previewClient, previewOptions, type AiOptions, type ExposurePreview as Preview, type PreviewTool } from "./api";
import { JsonWithPlaceholders } from "./PlaceholderText";

function ToolSample({ tool }: { tool: PreviewTool }) {
  const [open, setOpen] = useState(false);
  const marked = tool.placeholders.length;
  return (
    <details
      className="ai-tool"
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
      }}
    >
      <summary>
        <code>{tool.tool}</code>{" "}
        {tool.status >= 400 ? <Badge tone="warning">Refused {String(tool.status)}</Badge> : null}{" "}
        <span className="auth-muted">
          {String(tool.bytes)} bytes, {String(marked)} scrubbed {marked === 1 ? "field" : "fields"}
        </span>
      </summary>
      {open ? (
        <>
          <JsonWithPlaceholders value={tool.params} label={`Parameters of ${tool.tool}`} />
          <JsonWithPlaceholders value={tool.body} label={`Sample answer of ${tool.tool}`} />
        </>
      ) : null}
    </details>
  );
}

export function PreviewResult({ preview }: { preview: Preview }) {
  const counts = Object.entries(preview.placeholder_counts);
  return (
    <div className="settings-stack">
      <p>
        {preview.tools.length} tools run on your real data. Mode: <strong>{preview.mode}</strong>.{" "}
        {counts.length === 0 ? (
          "The scrubber changed nothing in these samples."
        ) : (
          <>
            Removed by the scrubber:{" "}
            {counts.map(([name, count]) => (
              <Badge key={name} tone="info" className="ai-count">
                {name} x{count}
              </Badge>
            ))}
          </>
        )}
      </p>
      <div className="ai-tools">
        {preview.tools.map((tool) => (
          <ToolSample key={tool.tool} tool={tool} />
        ))}
      </div>
    </div>
  );
}

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(value);
    }, ms);
    return () => {
      clearTimeout(timer);
    };
  }, [value, ms]);
  return debounced;
}

/** Live preview of unsaved options; re-runs shortly after the options change. */
export function LivePreview({ options }: { options: AiOptions | null }) {
  const key = JSON.stringify(options);
  const settled = useDebounced(key, 400);
  const query = useQuery({
    queryKey: [...queryRoots.settings, "ai", "preview", settled],
    queryFn: () => previewOptions(JSON.parse(settled) as AiOptions),
    enabled: options !== null && settled !== "null",
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  if (options === null) {
    return <p className="auth-muted">Fix the highlighted options to see the preview.</p>;
  }
  if (query.isPending || settled !== key) {
    return <Spinner label="Running every tool on your data" />;
  }
  if (query.isError) {
    return (
      <p className="auth-error" role="alert">
        {authErrorMessage(query.error)}
      </p>
    );
  }
  return <PreviewResult preview={query.data} />;
}

/** Preview of a saved client with its stored options. */
export function ClientPreview({ clientId }: { clientId: string }) {
  const query = useQuery({
    queryKey: [...queryRoots.settings, "ai", "client-preview", clientId],
    queryFn: () => previewClient(clientId),
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  if (query.isPending) {
    return <Spinner label="Running every tool on your data" />;
  }
  if (query.isError) {
    return (
      <p className="auth-error" role="alert">
        {authErrorMessage(query.error)}
      </p>
    );
  }
  return <PreviewResult preview={query.data} />;
}
