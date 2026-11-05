// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Live updates: `GET /api/v1/events` (Server-Sent Events) mapped onto
 * TanStack Query invalidations (docs/ARCHITECTURE.md, "Frontend").
 *
 * Protocol: each household event (`backend/app/core/events.py`) is sent as
 * an unnamed SSE `message` whose JSON data carries its `type`
 * (`data: {"id": "...", "type": "document.updated"}`). Named events
 * (`event: document.updated`) are handled too, for the exact types listed in
 * the mapping or in `namedTypes`: `EventSource` dispatches only the names a
 * listener was registered for, so wildcards cannot match named events. Event
 * data is never rendered; only the type is used, to decide which query
 * families to refetch.
 *
 * Mapping keys are exact types or prefixes ending in `.*` (`document.*`
 * matches `document.updated`). Unknown types are ignored.
 *
 * Reconnects: the stream is closed on any error and reopened after an
 * exponential backoff with jitter (1 s doubling to 30 s), reset on a
 * successful open. After a reconnect every mapped family is invalidated,
 * since events may have been missed while disconnected.
 */
import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { queryRoots } from "./queryKeys";

export const EVENTS_PATH = "/api/v1/events";

export type EventMapping = Readonly<Record<string, readonly QueryKey[]>>;

export const DEFAULT_EVENT_MAPPING: EventMapping = {
  "document.*": [
    queryRoots.documents,
    queryRoots.review,
    queryRoots.transactions,
    queryRoots.accounts,
    queryRoots.dashboard,
  ],
  "review.*": [queryRoots.review, queryRoots.documents, queryRoots.transactions, queryRoots.dashboard],
  "job.*": [queryRoots.jobs],
  "account.*": [queryRoots.accounts, queryRoots.netWorth, queryRoots.dashboard],
  "account_grant.*": [queryRoots.accounts, queryRoots.transactions, queryRoots.settings],
  "balance.*": [queryRoots.accounts, queryRoots.netWorth, queryRoots.dashboard],
  "transaction.*": [
    queryRoots.transactions,
    queryRoots.accounts,
    queryRoots.budgets,
    queryRoots.goals,
    queryRoots.reports,
    queryRoots.dashboard,
  ],
  "budget.*": [queryRoots.budgets, queryRoots.dashboard],
  "goal.*": [queryRoots.goals, queryRoots.dashboard],
  "rule.*": [queryRoots.rules],
  "category.*": [queryRoots.taxonomy, queryRoots.transactions, queryRoots.budgets, queryRoots.reports],
  "payee.*": [queryRoots.taxonomy, queryRoots.transactions, queryRoots.reports],
  "tag.*": [queryRoots.taxonomy, queryRoots.transactions],
  "member.*": [queryRoots.settings],
  "household.*": [queryRoots.settings, queryRoots.session],
  "investment.*": [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard],
  "holding.*": [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard],
  "price.*": [queryRoots.investments, queryRoots.netWorth, queryRoots.dashboard],
  "loan.*": [queryRoots.loans, queryRoots.netWorth, queryRoots.dashboard],
  "net_worth.*": [queryRoots.netWorth, queryRoots.dashboard],
  "networth.*": [queryRoots.netWorth, queryRoots.dashboard],
  "security.*": [queryRoots.investments],
  "asset.*": [queryRoots.assets, queryRoots.accounts, queryRoots.netWorth, queryRoots.dashboard],
  "schedule.*": [queryRoots.recurring],
  "fx.*": [queryRoots.investments, queryRoots.loans, queryRoots.netWorth, queryRoots.dashboard],
  "tax.*": [queryRoots.tax],
  "settings.*": [queryRoots.settings],
  "notification.*": [queryRoots.notifications],
  "session.*": [queryRoots.session],
};

const TYPE_RE = /^[a-z][a-z0-9_.:-]{0,99}$/;

function keyId(key: QueryKey): string {
  return JSON.stringify(key);
}

/** The query keys to invalidate for an event `type` (deduplicated, in mapping order). */
export function invalidationsFor(type: string, mapping: EventMapping = DEFAULT_EVENT_MAPPING): QueryKey[] {
  if (!TYPE_RE.test(type)) {
    return [];
  }
  const seen = new Set<string>();
  const result: QueryKey[] = [];
  for (const [pattern, keys] of Object.entries(mapping)) {
    const matches = pattern.endsWith(".*") ? type.startsWith(pattern.slice(0, -1)) : pattern === type;
    if (!matches) {
      continue;
    }
    for (const key of keys) {
      const id = keyId(key);
      if (!seen.has(id)) {
        seen.add(id);
        result.push(key);
      }
    }
  }
  return result;
}

/** Every distinct key in a mapping (used after a reconnect). */
export function allMappedKeys(mapping: EventMapping = DEFAULT_EVENT_MAPPING): QueryKey[] {
  const seen = new Set<string>();
  const result: QueryKey[] = [];
  for (const keys of Object.values(mapping)) {
    for (const key of keys) {
      const id = keyId(key);
      if (!seen.has(id)) {
        seen.add(id);
        result.push(key);
      }
    }
  }
  return result;
}

/** Event type carried in an unnamed `message` event's JSON data, if any. */
export function typeFromMessageData(data: unknown): string | null {
  if (typeof data !== "string" || data.length > 64 * 1024) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed === "object" && parsed !== null && "type" in parsed) {
      const { type } = parsed;
      return typeof type === "string" && TYPE_RE.test(type) ? type : null;
    }
  } catch {
    return null;
  }
  return null;
}

export interface BackoffOptions {
  initialMs: number;
  maxMs: number;
  factor: number;
  /** Fraction of the delay removed at random (0 = no jitter). */
  jitter: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = { initialMs: 1_000, maxMs: 30_000, factor: 2, jitter: 0.2 };

/** Delay before reconnect attempt `attempt` (0-based). */
export function backoffDelay(attempt: number, options: BackoffOptions = DEFAULT_BACKOFF, random: () => number = Math.random): number {
  const base = Math.min(options.maxMs, options.initialMs * options.factor ** Math.max(0, attempt));
  const jitter = Math.min(Math.max(options.jitter, 0), 1) * Math.min(Math.max(random(), 0), 1);
  return Math.round(base * (1 - jitter));
}

/** The subset of `EventSource` this module uses (lets tests inject a fake). */
export interface EventSourceLike {
  readonly readyState: number;
  onopen: ((this: EventSourceLike, ev: Event) => unknown) | null;
  onerror: ((this: EventSourceLike, ev: Event) => unknown) | null;
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export type StreamStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface EventStreamOptions {
  queryClient: Pick<QueryClient, "invalidateQueries">;
  url?: string;
  mapping?: EventMapping;
  backoff?: BackoffOptions;
  random?: () => number;
  createEventSource?: (url: string) => EventSourceLike;
  onStatus?: (status: StreamStatus) => void;
  /** Extra exact event names to listen for when the server uses named events. */
  namedTypes?: readonly string[];
}

/** Event names the backend is expected to emit (named-event form). */
export const DEFAULT_NAMED_TYPES: readonly string[] = [
  "document.created",
  "document.updated",
  "review.created",
  "review.updated",
  "job.updated",
  "account.updated",
  "balance.updated",
  "transaction.created",
  "transaction.updated",
  "transaction.deleted",
  "rule.updated",
  "goal.updated",
  "budget.updated",
  "net_worth.updated",
];

export interface EventStream {
  close(): void;
}

function defaultEventSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

/** Open the event stream; returns a handle whose `close()` stops it for good. */
export function startEventStream(options: EventStreamOptions): EventStream {
  const mapping = options.mapping ?? DEFAULT_EVENT_MAPPING;
  const url = options.url ?? EVENTS_PATH;
  const create = options.createEventSource ?? defaultEventSource;
  const exactTypes = [
    ...new Set([
      ...Object.keys(mapping).filter((pattern) => !pattern.endsWith(".*")),
      ...(options.namedTypes ?? DEFAULT_NAMED_TYPES),
    ]),
  ].filter((type) => TYPE_RE.test(type) && type !== "message");
  let source: EventSourceLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let hadFailure = false;
  let stopped = false;

  const invalidate = (keys: readonly QueryKey[]): void => {
    for (const queryKey of keys) {
      void options.queryClient.invalidateQueries({ queryKey });
    }
  };

  const handle = (type: string | null): void => {
    if (type !== null) {
      invalidate(invalidationsFor(type, mapping));
    }
  };

  const connect = (): void => {
    if (stopped) {
      return;
    }
    options.onStatus?.(hadFailure ? "reconnecting" : "connecting");
    const es = create(url);
    source = es;
    es.onopen = () => {
      attempt = 0;
      options.onStatus?.("open");
      if (hadFailure) {
        hadFailure = false;
        invalidate(allMappedKeys(mapping));
      }
    };
    es.onerror = () => {
      es.close();
      if (source !== es || stopped) {
        return;
      }
      source = null;
      hadFailure = true;
      options.onStatus?.("reconnecting");
      const delay = backoffDelay(attempt, options.backoff, options.random);
      attempt += 1;
      timer = setTimeout(() => {
        timer = null;
        connect();
      }, delay);
    };
    es.addEventListener("message", (event) => {
      handle(typeFromMessageData(event.data));
    });
    for (const type of exactTypes) {
      es.addEventListener(type, () => {
        handle(type);
      });
    }
  };

  connect();

  return {
    close() {
      stopped = true;
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      source?.close();
      source = null;
      options.onStatus?.("closed");
    },
  };
}
