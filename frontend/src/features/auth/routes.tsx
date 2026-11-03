// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoute, type AnyRoute } from "@tanstack/react-router";

import { EnrollMfaPage } from "./EnrollMfaPage";
import { ENROLL_PATH, FORGOT_PATH, LINK_OIDC_PATH, LOGIN_PATH, MFA_PATH, RESET_PATH } from "./guard";
import { LinkOidcPage } from "./LinkOidcPage";
import { LoginPage } from "./LoginPage";
import { MfaPage } from "./MfaPage";
import { ForgotPasswordPage, ResetPasswordPage } from "./PasswordResetPages";

export function routes(parent: AnyRoute): AnyRoute[] {
  const page = (path: string, component: () => React.JSX.Element) =>
    createRoute({ getParentRoute: () => parent, path, component });
  return [
    page(LOGIN_PATH, LoginPage),
    page(MFA_PATH, MfaPage),
    page(ENROLL_PATH, EnrollMfaPage),
    page(LINK_OIDC_PATH, LinkOidcPage),
    page(FORGOT_PATH, ForgotPasswordPage),
    page(RESET_PATH, ResetPasswordPage),
  ];
}
