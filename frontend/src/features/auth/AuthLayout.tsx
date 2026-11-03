// SPDX-License-Identifier: AGPL-3.0-or-later
import "./auth.css";

import { useRouter, useRouterState } from "@tanstack/react-router";
import { useCallback, useId, type ReactNode } from "react";

/** The centred card the sign-in, setup and reset pages share. */
export function AuthLayout({ title, intro, children }: { title: string; intro?: ReactNode; children: ReactNode }) {
  const headingId = useId();
  return (
    <section className="auth-page" aria-labelledby={headingId}>
      <div className="auth-card">
        <p className="auth-brand" aria-hidden="true">
          PerFin
        </p>
        <h1 id={headingId} className="auth-title">
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
