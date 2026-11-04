// SPDX-License-Identifier: AGPL-3.0-or-later
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { authedSession, json, mockApi, renderApp } from "../../auth/testing";
import type { Channel } from "./api";
import { buildCreate, buildUpdate, describeOutcome, emptyForm, formFrom } from "./form";

const NTFY: Channel = {
  id: "0190a000-0000-7000-8000-000000000001",
  name: "Phone",
  type: "ntfy",
  enabled: true,
  events: ["account_locked", "role_changed"],
  config: { server_url: "https://ntfy.example.org", priority: 3 },
  secrets_set: ["topic", "token"],
  last_sent_at: null,
  last_outcome: null,
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-01T10:00:00Z",
};

describe("notification form helpers", () => {
  it("validates each service and sends credentials only when typed", () => {
    const webhook = { ...emptyForm("webhook"), name: "Hook", url: "http://hooks.example.org" };
    expect(buildCreate(webhook).errors["url"]).toBeDefined();
    expect(buildCreate({ ...webhook, url: "https://hooks.example.org/x" }).body).toEqual({
      name: "Hook",
      enabled: true,
      events: emptyForm().kinds,
      config: { type: "webhook", url: "https://hooks.example.org/x" },
    });
    const smtp = { ...emptyForm("smtp"), name: "Mail", host: "smtp.example.org", sender: "perfin@example.org", recipients: "a@example.org, b@example.org" };
    expect(buildCreate(smtp).body?.config).toMatchObject({ type: "smtp", port: 587, tls: "starttls", recipients: ["a@example.org", "b@example.org"] });
    expect(buildCreate({ ...smtp, recipients: "nobody" }).errors["recipients"]).toBeDefined();
    expect(buildCreate({ ...emptyForm("gotify"), name: "G", serverUrl: "https://g.example.org" }).errors["token"]).toBeDefined();
    expect(buildCreate({ ...emptyForm("matrix"), name: "M", homeserverUrl: "https://m.example.org", roomId: "#alias:m.org", accessToken: "t" }).errors["roomId"]).toBeDefined();

    const form = formFrom(NTFY);
    expect(form.topic).toBe("");
    const update = buildUpdate(form, NTFY).body;
    expect(update?.config).toEqual({ type: "ntfy", server_url: "https://ntfy.example.org", priority: 3 });
    expect(buildUpdate({ ...form, token: "new" }, NTFY).body?.config).toHaveProperty("token", "new");
    expect(buildCreate({ ...emptyForm("ntfy"), name: "x" }).errors["topic"]).toBeDefined();
  });

  it("explains outcomes", () => {
    expect(describeOutcome(null)).toBe("Nothing sent yet.");
    expect(describeOutcome("tls_failed")).toMatch(/certificate/);
  });
});

describe("notifications settings page", () => {
  it("lists channels, sends a test and adds a webhook whose secret is shown once", async () => {
    const api = mockApi({
      "GET /api/v1/auth/session": authedSession({ role: "owner" }),
      "GET /api/v1/settings/notifications/channels": () => json([NTFY]),
      "POST /api/v1/settings/notifications/channels/:id/test": () => json({ ok: false, outcome: "auth_failed" }),
      "POST /api/v1/settings/notifications/channels": () =>
        json(
          {
            ...NTFY,
            id: "0190a000-0000-7000-8000-000000000002",
            name: "Hook",
            type: "webhook",
            config: { url: "https://hooks.example.org/x" },
            secrets_set: ["secret"],
            signing_secret: "synthetic-secret-shown-once",
          },
          201,
        ),
    });
    renderApp("/settings/notifications");
    const user = userEvent.setup();
    expect(await screen.findByRole("heading", { name: "Phone" })).toBeInTheDocument();
    expect(screen.getByText(/never includes names/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send a test to Phone" }));
    expect(await screen.findByText("Test: The service refused the credentials.")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Name"), "Hook");
    await user.selectOptions(screen.getByLabelText("Service"), "webhook");
    await user.type(screen.getByLabelText("Webhook URL"), "https://hooks.example.org/x");
    await user.click(screen.getByRole("button", { name: "Add channel" }));
    expect(await screen.findByTestId("signing-secret")).toHaveTextContent("synthetic-secret-shown-once");
    await waitFor(() => {
      expect(api.callsTo("POST", "/api/v1/settings/notifications/channels")).toHaveLength(1);
    });
  });

  it("is closed to a member", async () => {
    const api = mockApi({ "GET /api/v1/auth/session": authedSession({ role: "member" }) });
    renderApp("/settings/notifications");
    expect(await screen.findByText(/Only a household owner or administrator/)).toBeInTheDocument();
    expect(api.callsTo("GET", "/api/v1/settings/notifications/channels")).toHaveLength(0);
  });
});
