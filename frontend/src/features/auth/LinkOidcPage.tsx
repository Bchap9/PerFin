// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * "Link by verified email" (docs/AUTH.md, OIDC): the identity provider
 * vouched for an email that matches an existing PerFin account. The link is
 * only made after a fresh local sign-in to that account (password, plus the
 * second factor when it has one), so whoever controls the IdP account cannot
 * take over the PerFin one by email alone.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Spinner } from "../../components/Spinner";
import { isApiError } from "../../lib/api/client";
import { confirmLink, discardLink, getLinkOffer } from "./api";
import { AuthLayout, useGoTo } from "./AuthLayout";
import { authErrorMessage } from "./errors";
import { LINK_OIDC_PATH, LOGIN_PATH } from "./guard";
import { LocalLoginForm } from "./LoginPage";
import { useAuthSession } from "./session";

export function LinkOidcPage() {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const offer = useQuery({ queryKey: ["auth", "oidc-link"], queryFn: getLinkOffer, retry: false });
  const { state, session } = useAuthSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linked, setLinked] = useState(false);

  if (offer.isPending) {
    return (
      <AuthLayout title="Link your account">
        <Spinner label="Loading" />
      </AuthLayout>
    );
  }

  if (linked) {
    return (
      <AuthLayout title="Accounts linked">
        <p role="status">From now on you can sign in with single sign-on.</p>
        <Link to="/" className="btn btn--primary btn--md">
          Continue
        </Link>
      </AuthLayout>
    );
  }

  if (offer.isError) {
    const missing = isApiError(offer.error) && offer.error.status === 404;
    return (
      <AuthLayout title="Link your account">
        <p role="alert">{missing ? "There is no pending link, or it has expired. Sign in with single sign-on again." : authErrorMessage(offer.error)}</p>
        <Link to={LOGIN_PATH}>Back to sign in</Link>
      </AuthLayout>
    );
  }

  const { email } = offer.data;

  const confirm = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await confirmLink();
      await queryClient.invalidateQueries({ queryKey: ["auth"] });
      setLinked(true);
    } catch (caught) {
      setError(authErrorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const discard = async (): Promise<void> => {
    setBusy(true);
    try {
      await discardLink();
    } catch {
      // The offer expires on its own.
    }
    await goTo(LOGIN_PATH, { replace: true });
  };

  return (
    <AuthLayout
      title="Link your account"
      intro={
        <p>
          Your identity provider signed you in as <strong>{email}</strong>, which matches an existing PerFin account.
        </p>
      }
    >
      {session === null ? (
        <>
          <p>To link them, sign in with that account&apos;s PerFin password first.</p>
          {state?.status === "mfa_pending" ? (
            <Link to={`/login/mfa?return_to=${encodeURIComponent(LINK_OIDC_PATH)}`}>Finish signing in</Link>
          ) : (
            <LocalLoginForm returnTo={LINK_OIDC_PATH} showForgot={false} />
          )}
        </>
      ) : (
        <>
          <p>
            Signed in as <strong>{session.user.username}</strong>. Link this account to single sign-on?
          </p>
          {error === null ? null : (
            <p className="auth-error" role="alert">
              {error}
            </p>
          )}
          <Button
            variant="primary"
            loading={busy}
            onClick={() => {
              void confirm();
            }}
          >
            Link accounts
          </Button>
        </>
      )}
      <Button
        variant="ghost"
        disabled={busy}
        onClick={() => {
          void discard();
        }}
      >
        Do not link
      </Button>
    </AuthLayout>
  );
}
