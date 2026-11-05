// SPDX-License-Identifier: AGPL-3.0-or-later
import { MfaSection } from "./security/MfaSection";
import { PasswordSection } from "./security/PasswordSection";
import { SessionsSection } from "./security/SessionsSection";
import { TokensSection } from "./security/TokensSection";
import { SettingsPage } from "./SettingsLayout";

export function SecurityPage() {
  return (
    <SettingsPage title="Security">
      <PasswordSection />
      <MfaSection />
      <SessionsSection />
      <TokensSection />
    </SettingsPage>
  );
}
