// SPDX-License-Identifier: AGPL-3.0-or-later
/** Pure helpers of the notification channels page: form state, request bodies and outcome texts. */
import type { Channel, ChannelCreate, ChannelType, ChannelUpdate, Kind, Outcome } from "./api";

export const KINDS: readonly { kind: Kind; label: string }[] = [
  { kind: "account_locked", label: "An account is locked after failed sign-ins" },
  { kind: "recovery_code_used", label: "A recovery code is used" },
  { kind: "mfa_reset", label: "An administrator resets someone's second factors" },
  { kind: "role_changed", label: "Someone is added, removed or changes role" },
  { kind: "new_device", label: "Someone signs in from a new device" },
  { kind: "ai_client_created", label: "An AI assistant connection is created" },
  { kind: "review_waiting", label: "Items are waiting in Review (daily)" },
  { kind: "tax_reminder", label: "Expected tax documents are still missing" },
  { kind: "backup_failed", label: "A backup failed" },
];

export const TYPES: readonly { value: ChannelType; label: string }[] = [
  { value: "smtp", label: "Email (SMTP)" },
  { value: "webhook", label: "Webhook (signed HTTPS POST)" },
  { value: "ntfy", label: "ntfy" },
  { value: "gotify", label: "Gotify" },
  { value: "matrix", label: "Matrix room" },
];

export interface ChannelForm {
  name: string;
  type: ChannelType;
  enabled: boolean;
  kinds: Kind[];
  host: string;
  port: string;
  tls: "starttls" | "implicit";
  sender: string;
  recipients: string;
  username: string;
  /** Write-only fields: blank keeps what is stored. */
  password: string;
  url: string;
  serverUrl: string;
  topic: string;
  token: string;
  priority: string;
  homeserverUrl: string;
  roomId: string;
  accessToken: string;
}

function defaultPriority(type: ChannelType): string {
  if (type === "ntfy") {
    return "3";
  }
  return type === "gotify" ? "5" : "";
}

export function emptyForm(type: ChannelType = "ntfy"): ChannelForm {
  return {
    name: "",
    type,
    enabled: true,
    kinds: KINDS.map((k) => k.kind),
    host: "",
    port: "587",
    tls: "starttls",
    sender: "",
    recipients: "",
    username: "",
    password: "",
    url: "",
    serverUrl: type === "ntfy" ? "https://ntfy.sh" : "",
    topic: "",
    token: "",
    priority: defaultPriority(type),
    homeserverUrl: "",
    roomId: "",
    accessToken: "",
  };
}

export function formFrom(channel: Channel): ChannelForm {
  const c = channel.config;
  const form = emptyForm(channel.type);
  return {
    ...form,
    name: channel.name,
    enabled: channel.enabled,
    kinds: [...channel.events],
    host: c.host ?? "",
    port: c.port === null || c.port === undefined ? form.port : String(c.port),
    tls: c.tls ?? "starttls",
    sender: c.sender ?? "",
    recipients: (c.recipients ?? []).join(", "),
    username: c.username ?? "",
    url: c.url ?? "",
    serverUrl: c.server_url ?? "",
    priority: c.priority === null || c.priority === undefined ? form.priority : String(c.priority),
    homeserverUrl: c.homeserver_url ?? "",
    roomId: c.room_id ?? "",
  };
}

export interface Built<T> {
  body: T | null;
  errors: Record<string, string>;
}

/** An https URL with a host and no credentials, spaces or fragment (the server checks it fully). */
function isHttps(value: string): boolean {
  if (!value.toLowerCase().startsWith("https://") || /[\s#@]/.test(value)) {
    return false;
  }
  try {
    return new URL(value).hostname !== "";
  } catch {
    return false;
  }
}
const ADDRESS = /^[^@\s<>,;]+@[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$/;
const TOPIC = /^[A-Za-z0-9_-]{1,64}$/;
const ROOM = /^![^\s:]+:\S+$/;

function splitAddresses(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

/** Whether a stored credential exists (so a blank field keeps it). */
type Stored = ReadonlySet<string>;

function configOf(form: ChannelForm, stored: Stored, errors: Record<string, string>): Record<string, unknown> {
  const missing = (value: string, key: string): boolean => value === "" && !stored.has(key);
  const optional = (value: string): string | undefined => (value.trim() === "" ? undefined : value.trim());
  switch (form.type) {
    case "smtp": {
      const port = Number(form.port);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        errors["port"] = "Enter a port from 1 to 65535.";
      }
      if (form.host.trim() === "") {
        errors["host"] = "Enter the mail server's host name.";
      }
      if (!ADDRESS.test(form.sender.trim())) {
        errors["sender"] = "Enter the sender's email address.";
      }
      const recipients = splitAddresses(form.recipients);
      if (recipients.length === 0 || recipients.length > 10 || !recipients.every((r) => ADDRESS.test(r))) {
        errors["recipients"] = "Enter one to ten email addresses.";
      }
      return {
        type: "smtp",
        host: form.host.trim(),
        port,
        tls: form.tls,
        sender: form.sender.trim(),
        recipients,
        username: optional(form.username),
        password: form.password === "" ? undefined : form.password,
      };
    }
    case "webhook":
      if (!isHttps(form.url.trim())) {
        errors["url"] = "Enter an https:// address.";
      }
      return { type: "webhook", url: form.url.trim() };
    case "ntfy":
      if (!isHttps(form.serverUrl.trim())) {
        errors["serverUrl"] = "Enter the server's https:// address.";
      }
      if (form.topic !== "" && !TOPIC.test(form.topic)) {
        errors["topic"] = "Letters, digits, - and _ only (up to 64).";
      }
      if (missing(form.topic, "topic")) {
        errors["topic"] = "Enter the topic.";
      }
      return {
        type: "ntfy",
        server_url: form.serverUrl.trim(),
        topic: form.topic === "" ? undefined : form.topic,
        token: form.token === "" ? undefined : form.token,
        priority: Number(form.priority),
      };
    case "gotify":
      if (!isHttps(form.serverUrl.trim())) {
        errors["serverUrl"] = "Enter the server's https:// address.";
      }
      if (missing(form.token, "token")) {
        errors["token"] = "Enter the application token.";
      }
      return {
        type: "gotify",
        server_url: form.serverUrl.trim(),
        token: form.token === "" ? undefined : form.token,
        priority: Number(form.priority),
      };
    case "matrix":
      if (!isHttps(form.homeserverUrl.trim())) {
        errors["homeserverUrl"] = "Enter the homeserver's https:// address.";
      }
      if (!ROOM.test(form.roomId.trim())) {
        errors["roomId"] = "Enter the room id, for example !abc123:example.org.";
      }
      if (missing(form.accessToken, "access_token")) {
        errors["accessToken"] = "Enter the access token.";
      }
      return {
        type: "matrix",
        homeserver_url: form.homeserverUrl.trim(),
        room_id: form.roomId.trim(),
        access_token: form.accessToken === "" ? undefined : form.accessToken,
      };
  }
}

function withoutUndefined(config: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(config).filter(([, value]) => value !== undefined));
}

function checkName(form: ChannelForm, errors: Record<string, string>): void {
  if (form.name.trim() === "") {
    errors["name"] = "Give the channel a name.";
  }
}

/** The POST body of a new channel. */
export function buildCreate(form: ChannelForm): Built<ChannelCreate> {
  const errors: Record<string, string> = {};
  checkName(form, errors);
  const config = withoutUndefined(configOf(form, new Set(), errors));
  if (Object.keys(errors).length > 0) {
    return { body: null, errors };
  }
  return {
    errors,
    body: { name: form.name.trim(), enabled: form.enabled, events: form.kinds, config: config as ChannelCreate["config"] },
  };
}

/** The PATCH body of an existing channel; credentials are only sent when typed. */
export function buildUpdate(form: ChannelForm, channel: Channel): Built<ChannelUpdate> {
  const errors: Record<string, string> = {};
  checkName(form, errors);
  const config = withoutUndefined(configOf(form, new Set(channel.secrets_set), errors));
  if (Object.keys(errors).length > 0) {
    return { body: null, errors };
  }
  return { errors, body: { name: form.name.trim(), enabled: form.enabled, events: form.kinds, config } };
}

const OUTCOMES: Readonly<Record<Outcome, string>> = {
  ok: "Delivered.",
  blocked: "This destination is not allowed (private or local addresses, or a host the administrator has not listed).",
  tls_failed: "The service's certificate or TLS setup was not accepted.",
  auth_failed: "The service refused the credentials.",
  rejected: "The service refused the message.",
  connect_failed: "Could not reach the service.",
  timeout: "The service took too long to answer.",
  server_error: "The service had a temporary problem.",
  protocol_error: "The service answered in a way PerFin could not use.",
};

export function describeOutcome(outcome: Outcome | null | undefined): string {
  if (outcome === null || outcome === undefined) {
    return "Nothing sent yet.";
  }
  return new Map(Object.entries(OUTCOMES)).get(outcome) ?? outcome;
}

export function typeLabel(type: ChannelType): string {
  return TYPES.find((t) => t.value === type)?.label ?? type;
}
