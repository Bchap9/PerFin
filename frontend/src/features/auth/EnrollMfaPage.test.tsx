// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, currentHref, json, mockApi } from "./testing";
import { renderApp } from "./testing";

vi.mock("@simplewebauthn/browser", () => ({
  browserSupportsWebAuthn: () => false,
  startAuthentication: vi.fn(),
  startRegistration: vi.fn(),
}));

const URI = "otpauth://totp/PerFin:alice?secret=JBSWY3DPEHPK3PXP&issuer=PerFin";

describe("enrolment-only flow", () => {
  it("sends a restricted session to enrolment and finishes after the recovery codes", async () => {
    let enrolled = false;
    const api = mockApi({
      "GET /api/v1/auth/session": (call) => authedSession({ enrollmentRequired: !enrolled, enrolled })(call),
      "POST /api/v1/auth/mfa/totp": () =>
        json({ secret: "JBSWY3DPEHPK3PXP", otpauth_uri: URI, digits: 6, period: 30, algorithm: "SHA1", expires_at: "2026-11-03T10:10:00Z" }, 201),
      "POST /api/v1/auth/mfa/totp/confirm": () => {
        enrolled = true;
        return json(
          {
            method: { id: "m-1", type: "totp", name: "Phone", created_at: "2026-11-03T10:00:00Z", last_used_at: null },
            mfa_verified_at: "2026-11-03T10:00:00Z",
            csrf_token: "rotated-token",
            recovery_codes: ["AAAA-BBBB-CCCC-DDDD", "EEEE-FFFF-GGGG-HHHH"],
          },
          201,
        );
      },
    });
    const { router } = renderApp("/budgets");
    await waitFor(() => {
      expect(currentHref(router)).toBe("/login/enroll-mfa?return_to=%2Fbudgets");
    });
    expect(screen.queryByRole("navigation", { name: "Main" })).not.toBeInTheDocument();
    expect(await screen.findByText(/This browser cannot register security keys/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Set up authenticator app" }));
    // The QR code is rendered locally into a data: URL; the key is also shown as text.
    const qr = await screen.findByRole("img", { name: "QR code for your authenticator app" });
    expect(qr.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
    expect(screen.getByLabelText("Setup key")).toHaveTextContent("JBSW Y3DP EHPK 3PXP");

    await user.type(screen.getByLabelText(/^Code from the app/), "654321");
    await user.clear(screen.getByLabelText(/^Name/));
    await user.type(screen.getByLabelText(/^Name/), "Phone");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    const confirm = api.callsTo("POST", "/api/v1/auth/mfa/totp/confirm")[0];
    expect(confirm?.body).toEqual({ code: "654321", name: "Phone" });
    expect(confirm?.headers.get("X-CSRF-Token")).toBe("session-token");

    // Recovery codes are shown once and need an acknowledgement.
    const dialog = await screen.findByRole("dialog", { name: "Save your recovery codes" });
    expect(within(dialog).getByText("AAAA-BBBB-CCCC-DDDD")).toBeInTheDocument();
    const done = within(dialog).getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await user.click(within(dialog).getByLabelText(/I have stored this somewhere safe/));
    await user.click(done);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/budgets");
    });
    expect(screen.queryByText("AAAA-BBBB-CCCC-DDDD")).not.toBeInTheDocument();
  });
});
