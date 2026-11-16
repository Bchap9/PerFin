// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Every old address answers with its new one (`REDIRECTS` in routeMap.ts),
 * replacing the history entry and keeping the search and hash. Two rows first
 * read the object to find its account (`data`): a loan opens its account's
 * Schedule tab, an asset its Valuations tab. An id that is gone says so, with
 * a link to Accounts, and never shows a blank page.
 *
 * The data rows call the API directly so no feature module is imported here
 * (plan, Decision 4).
 */
import { useQuery } from "@tanstack/react-query";
import { createRoute, Link, redirect, useNavigate, useRouterState, type AnyRoute } from "@tanstack/react-router";
import { useEffect } from "react";

import { Spinner } from "../components/Spinner";
import { api, errorMessage, isApiError, unwrap } from "../lib/api/client";
import { queryRoots } from "../lib/queryKeys";
import { useRouteParam } from "../features/planning/useRouteParam";
import { REDIRECTS, type Redirect } from "./routeMap";

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function redirectRoute(root: AnyRoute, row: Redirect): AnyRoute {
  return createRoute({
    getParentRoute: () => root,
    path: row.from,
    beforeLoad: ({ location, params }) => {
      // The thrown redirect is the router's own control flow, not an error.
      // eslint-disable-next-line @typescript-eslint/only-throw-error
      throw redirect({
        to: row.to,
        params: asRecord(params),
        search: { ...asRecord(location.search), ...row.search },
        hash: location.hash,
        replace: true,
      });
    },
  });
}

const KIND = new Map([
  ["loan", { param: "loanId", noun: "loan", Noun: "Loan" }],
  ["asset", { param: "assetId", noun: "asset", Noun: "Asset" }],
] as const);

function readAccountId(kind: "loan" | "asset", id: string): Promise<string> {
  if (kind === "loan") {
    return unwrap(api.GET("/api/v1/loans/{loan_id}", { params: { path: { loan_id: id } } })).then((loan) => loan.account_id);
  }
  return unwrap(api.GET("/api/v1/assets/{asset_id}", { params: { path: { asset_id: id } } })).then((asset) => asset.account_id);
}

/** Reads the loan or asset, then replaces the address with its account's tab. */
export function AccountRedirect({ kind, tab }: { kind: "loan" | "asset"; tab: string }) {
  const { param, noun, Noun } = KIND.get(kind) ?? { param: "loanId", noun: "loan", Noun: "Loan" };
  const id = useRouteParam(param);
  const navigate = useNavigate();
  const location = useRouterState({ select: (state) => state.location });
  const query = useQuery({
    queryKey: [...(kind === "loan" ? queryRoots.loans : queryRoots.assets), "account-of", id],
    queryFn: () => readAccountId(kind, id),
    enabled: id !== "",
    retry: false,
  });
  const accountId = query.data;

  useEffect(() => {
    if (accountId === undefined) {
      return;
    }
    void navigate({
      to: "/accounts/$accountId",
      params: { accountId },
      search: { ...asRecord(location.search), tab },
      hash: location.hash,
      replace: true,
    });
  }, [accountId, location.hash, location.search, navigate, tab]);

  if (query.isError) {
    const gone = isApiError(query.error) && query.error.status === 404;
    return (
      <section className="status-page" aria-labelledby="redirect-title">
        <h1 id="redirect-title">{gone ? `${Noun} not found` : `Could not open the ${noun}`}</h1>
        <p>{gone ? `This ${noun} no longer exists.` : errorMessage(query.error)}</p>
        <Link to="/accounts" className="btn btn--primary btn--md">
          Go to Accounts
        </Link>
      </section>
    );
  }
  return (
    <div className="status-page">
      <Spinner size="lg" label={`Opening the ${noun}'s account`} />
    </div>
  );
}

function dataRoute(root: AnyRoute, row: Redirect & { data: "loan" | "asset" }): AnyRoute {
  const tab = row.search?.["tab"] ?? "overview";
  const kind = row.data;
  return createRoute({
    getParentRoute: () => root,
    path: row.from,
    component: function DataRedirect() {
      return <AccountRedirect kind={kind} tab={tab} />;
    },
  });
}

/** One route per `REDIRECTS` row. */
export function redirectRoutes(root: AnyRoute): AnyRoute[] {
  return REDIRECTS.map((row) => (row.data === undefined ? redirectRoute(root, row) : dataRoute(root, { ...row, data: row.data })));
}
