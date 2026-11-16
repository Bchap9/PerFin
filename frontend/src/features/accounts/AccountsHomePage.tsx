// SPDX-License-Identifier: AGPL-3.0-or-later
// Stage 3 bridge: lane A replaces this.
import { useSearch } from "@tanstack/react-router";

import { AssetsPage } from "../assets/AssetsPage";
import { LoansPage } from "../loans/LoansPage";
import { AccountsPage } from "./AccountsPage";

/**
 * Accounts (`/accounts`): today's account list, then the loans list for
 * `?group=loans` (the old `/loans`) or the assets list for `?group=property`
 * (the old `/assets`).
 */
export function AccountsHomePage() {
  const search: Record<string, unknown> = useSearch({ strict: false });
  const group = search["group"];
  return (
    <>
      <AccountsPage />
      {group === "loans" ? <LoansPage /> : null}
      {group === "property" ? <AssetsPage /> : null}
    </>
  );
}
