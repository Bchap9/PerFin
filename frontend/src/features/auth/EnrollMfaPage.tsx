// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Enrolment-only flow: with `PERFIN_REQUIRE_MFA` on, a user without a second
 * factor gets a session that can do nothing but enrol one (the server answers
 * `403 mfa-enrollment-required` everywhere else). The guard sends such
 * sessions here; once a factor is added (and the recovery codes have been
 * shown once) the browser continues to `return_to`.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { logout, type FactorAdded } from "./api";
import { AuthLayout, useGoTo, useLocationParts } from "./AuthLayout";
import { LOGIN_PATH } from "./guard";
import { TotpSetup } from "./mfa/TotpSetup";
import { WebAuthnSetup } from "./mfa/WebAuthnSetup";
import { returnToFromSearch } from "./returnTo";
import { sessionChanged, sessionEnded } from "./session";
import { ShowOnceDialog } from "./ShowOnceDialog";

export function EnrollMfaPage() {
  const queryClient = useQueryClient();
  const goTo = useGoTo();
  const { search } = useLocationParts();
  const returnTo = returnToFromSearch(search);
  const [codes, setCodes] = useState<string[] | null>(null);

  const finish = async (): Promise<void> => {
    setCodes(null);
    await sessionChanged(queryClient);
    await goTo(returnTo, { replace: true });
  };

  const onAdded = (added: FactorAdded): void => {
    if (added.recovery_codes !== undefined && added.recovery_codes !== null && added.recovery_codes.length > 0) {
      setCodes(added.recovery_codes);
    } else {
      void finish();
    }
  };

  const signOut = async (): Promise<void> => {
    try {
      await logout();
    } finally {
      await sessionEnded(queryClient);
      await goTo(LOGIN_PATH, { replace: true });
    }
  };

  return (
    <AuthLayout
      title="Set up a second factor"
      intro={
        <p>
          This PerFin server requires a second factor. Add a security key, a passkey or an authenticator app to
          continue.
        </p>
      }
    >
      <Card title="Security key or passkey" headingLevel={2}>
        <WebAuthnSetup onAdded={onAdded} />
      </Card>
      <Card title="Authenticator app" headingLevel={2}>
        <TotpSetup onAdded={onAdded} />
      </Card>
      <Button
        variant="ghost"
        onClick={() => {
          void signOut();
        }}
      >
        Sign out
      </Button>
      <ShowOnceDialog
        open={codes !== null}
        title="Save your recovery codes"
        description="Each code signs you in once if you lose your second factor. They are shown only now."
        lines={codes ?? []}
        label="recovery codes"
        onDone={() => {
          void finish();
        }}
      />
    </AuthLayout>
  );
}
