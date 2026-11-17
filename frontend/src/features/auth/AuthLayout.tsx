// SPDX-License-Identifier: AGPL-3.0-or-later
import "./auth.css";

import { useRouter, useRouterState } from "@tanstack/react-router";
import { useCallback, useEffect, type ReactNode } from "react";

import { PAGE_TITLE_ID } from "../../components/PageHeader";

/** The centred card the sign-in, setup and reset pages share. */
export function AuthLayout({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  // The document title (WCAG 2.4.2); RootLayout names the other pages.
  useEffect(() => {
    document.title = `${title} · PerFin`;
  }, [title]);
  return (
    <section className="auth-page" aria-labelledby={PAGE_TITLE_ID}>
      <div className="auth-card">
        <p className="auth-brand" aria-hidden="true">
          PerFin
        </p>
        <h1 id={PAGE_TITLE_ID} className="auth-title">
          {title}
        </h1>
        {intro === undefined ? null : <div className="auth-intro">{intro}</div>}
        {children}
      </div>
    </section>
  );
}

/** The current location's raw query string and fragment (without `#`). */
export function useLocationParts(): { pathname: string; search: string; hash: string } {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const search = useRouterState({ select: (state) => state.location.searchStr });
  const hash = useRouterState({ select: (state) => state.location.hash });
  return { pathname, search, hash };
}

/** Navigate to an in-app href (already validated by the caller). */
export function useGoTo(): (href: string, options?: { replace?: boolean }) => Promise<void> {
  const router = useRouter();
  return useCallback(
    (href: string, options?: { replace?: boolean }) => router.navigate({ href, replace: options?.replace ?? false }),
    [router],
  );
}
