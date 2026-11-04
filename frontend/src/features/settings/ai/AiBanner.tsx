// SPDX-License-Identifier: AGPL-3.0-or-later
/** The persistent banner shown on every page while an AI client can read this household (SECURITY.md M5). */
import { Link } from "@tanstack/react-router";

import { useAuthSession } from "../../auth/session";

export function AiBanner() {
  const { session } = useAuthSession();
  if (session?.ai_clients_active !== true) {
    return null;
  }
  return (
    <div className="ai-banner" role="status" data-testid="ai-banner">
      <strong>AI access is on.</strong> An assistant can read financial facts of this household.{" "}
      <Link to="/settings/ai">Review or revoke</Link>
    </div>
  );
}
