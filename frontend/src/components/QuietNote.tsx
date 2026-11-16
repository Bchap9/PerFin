// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * A muted marker with a dot, for "not checked against a statement" and
 * "estimate": something a person should know, said quietly, in words.
 */
import type { ReactNode } from "react";

export function QuietNote({ children }: { children: ReactNode }) {
  return <span className="quiet">{children}</span>;
}
