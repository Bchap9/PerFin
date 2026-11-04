// SPDX-License-Identifier: AGPL-3.0-or-later
import { useParams } from "@tanstack/react-router";

/** A path parameter of the current route as a string ("" when absent). */
export function useRouteParam(name: string): string {
  const params: unknown = useParams({ strict: false });
  if (typeof params !== "object" || params === null) {
    return "";
  }
  const value = new Map<string, unknown>(Object.entries(params as Record<string, unknown>)).get(name);
  return typeof value === "string" ? value : "";
}
