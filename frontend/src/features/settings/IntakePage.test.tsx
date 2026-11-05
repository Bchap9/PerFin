// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../auth/testing";
import { buildUpdate, describeTest, formFrom } from "./intake";
import type { MailIntake } from "./api";

const INTAKE: MailIntake = {
  alias_rotated_at: "2026-11-01T10:00:00Z",
  alias_token: "tok-abc123",
  allowed_senders: [],
  authserv_id: null,
  configured: true,
  enabled: true,
  host: "imap.example.org",
  inbox_folder: "INBOX",
  instance_enabled: true,
  last_poll_at: null,
  last_poll_counts: { accepted: 2 },
  last_poll_outcome: "ok",
  member_addresses: true,
  password_set: true,
  port: 993,
  processed_folder: "PerFin/Done",
  rejected_folder: "PerFin/Rejected",
  require_auth_results: true,
  username: "receipts",
};

describe("intake form helpers", () => {
  it("sends the password only when typed and validates the port", () => {
    const form = formFrom(INTAKE);
    expect(buildUpdate(form).body).not.toHaveProperty("password");
    expect(buildUpdate({ ...form, password: "s3cret" }).body).toHaveProperty("password", "s3cret");
    expect(buildUpdate({ ...form, port: "0" }).errors["port"]).toBeDefined();
    expect(buildUpdate({ ...form, host: "" }).errors["host"]).toBeDefined();
    expect(buildUpdate({ ...form, senders: [{ address: "x", userId: "" }] }).body).toBeNull();
  });

  it("explains each outcome of the connection test", () => {
    expect(describeTest({ ok: true, outcome: "ok", messages: 3, move_supported: false })).toMatch(/3 message\(s\) waiting.*cannot move/);
    expect(describeTest({ ok: false, outcome: "auth_failed" })).toMatch(/refused the user name/);
  });
});

describe("intake settings page", () => {
  it("shows the alias, tests the connection and rotates the alias", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "owner" }),
      "GET /api/v1/settings/intake": () => json(INTAKE),
      "GET /api/v1/households/:id/members": () => json([]),
      "POST /api/v1/settings/intake/email/test": () => json({ ok: false, outcome: "tls_failed" }),
      "POST /api/v1/settings/intake/email/rotate-alias": () => json({ alias_token: "tok-new", alias_rotated_at: "2026-11-04T10:00:00Z" }),
    });
    renderApp("/settings/intake");
    const user = userEvent.setup();
    expect(await screen.findByTestId("alias-token")).toHaveTextContent("tok-abc123");
    expect(screen.getByLabelText("Password")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Test the connection" }));
    expect(await screen.findByText("The server's certificate was not accepted.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Replace the alias" }));
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/settings/intake/email/rotate-alias")).toHaveLength(1);
    });
  });

  it("is closed to a member", async () => {
    const api = mockApi({ "GET /api/v1/auth/session": authedSession({ role: "member" }) });
    renderApp("/settings/intake");
    expect(await screen.findByText(/Only a household owner or administrator/)).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/settings/intake")).toHaveLength(0);
  });
});
