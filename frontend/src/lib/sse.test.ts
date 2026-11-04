// SPDX-License-Identifier: AGPL-3.0-or-later
import type { QueryKey } from "@tanstack/react-query";

import {
  allMappedKeys,
  backoffDelay,
  DEFAULT_EVENT_MAPPING,
  invalidationsFor,
  startEventStream,
  typeFromMessageData,
  type EventSourceLike,
  type StreamStatus,
} from "./sse";

class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readyState = 0;
  onopen: EventSourceLike["onopen"] = null;
  onerror: EventSourceLike["onerror"] = null;
  closed = false;
  private readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.call(this, new Event("open"));
  }

  fail(): void {
    this.onerror?.call(this, new Event("error"));
  }

  emit(type: string, data = ""): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(new MessageEvent(type, { data }));
    }
  }
}

function setup() {
  FakeEventSource.instances = [];
  const invalidated: QueryKey[] = [];
  const statuses: StreamStatus[] = [];
  const queryClient = {
    invalidateQueries: vi.fn((filters?: { queryKey?: QueryKey }) => {
      if (filters?.queryKey !== undefined) {
        invalidated.push(filters.queryKey);
      }
      return Promise.resolve();
    }),
  };
  const stream = startEventStream({
    queryClient,
    createEventSource: (url) => new FakeEventSource(url),
    random: () => 0,
    onStatus: (status) => statuses.push(status),
  });
  const current = (): FakeEventSource => {
    const source = FakeEventSource.instances.at(-1);
    if (source === undefined) {
      throw new Error("no event source");
    }
    return source;
  };
  return { stream, invalidated, statuses, current };
}

describe("event type mapping", () => {
  it("maps event types to query-key invalidations", () => {
    expect(invalidationsFor("document.updated")).toEqual([
      ["documents"],
      ["review"],
      ["transactions"],
      ["accounts"],
      ["dashboard"],
    ]);
    expect(invalidationsFor("job.updated")).toEqual([["jobs"]]);
    expect(invalidationsFor("transaction.created")).toEqual([
      ["transactions"],
      ["accounts"],
      ["budgets"],
      ["goals"],
      ["reports"],
      ["dashboard"],
    ]);
  });

  it("supports exact and wildcard keys and deduplicates", () => {
    const mapping = { "a.b": [["x"]], "a.*": [["x"], ["y"]] };
    expect(invalidationsFor("a.b", mapping)).toEqual([["x"], ["y"]]);
    expect(invalidationsFor("a.c", mapping)).toEqual([["x"], ["y"]]);
    expect(invalidationsFor("ab", mapping)).toEqual([]);
  });

  it("ignores unknown and malformed types", () => {
    expect(invalidationsFor("unknown.thing")).toEqual([]);
    expect(invalidationsFor("Document.Updated")).toEqual([]);
    expect(invalidationsFor("")).toEqual([]);
  });

  it("lists every mapped key once", () => {
    const keys = allMappedKeys(DEFAULT_EVENT_MAPPING).map((key) => JSON.stringify(key));
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).toContain('["net-worth"]');
  });

  it("maps the Phase 2 ledger event families", () => {
    expect(invalidationsFor("rule.created")).toEqual([["rules"]]);
    expect(invalidationsFor("goal.updated")).toEqual([["goals"], ["dashboard"]]);
    expect(invalidationsFor("category.changed")).toContainEqual(["taxonomy"]);
    expect(invalidationsFor("transaction.rules_applied")).toContainEqual(["reports"]);
    expect(invalidationsFor("account_grant.changed")).toContainEqual(["transactions"]);
  });

  it("refreshes the ledger screens when imports and reviews change", () => {
    for (const type of ["document.updated", "review.resolved", "review.dismissed"]) {
      const keys = invalidationsFor(type).map((key) => JSON.stringify(key));
      expect(keys).toContain('["transactions"]');
      expect(keys).toContain('["review"]');
      expect(keys).toContain('["documents"]');
    }
    expect(invalidationsFor("balance.updated")).toContainEqual(["accounts"]);
    expect(invalidationsFor("transaction.transfer_linked")).toContainEqual(["transactions"]);
  });

  it("reads the type from unnamed message data", () => {
    expect(typeFromMessageData('{"id":"1","type":"review.created"}')).toBe("review.created");
    expect(typeFromMessageData('{"type":"<img src=x>"}')).toBeNull();
    expect(typeFromMessageData("not json")).toBeNull();
    expect(typeFromMessageData('["review.created"]')).toBeNull();
    expect(typeFromMessageData(42)).toBeNull();
  });
});

describe("backoff", () => {
  it("doubles from 1 s up to 30 s, with jitter only shortening the delay", () => {
    const none = () => 0;
    expect([0, 1, 2, 3, 4, 5, 6].map((attempt) => backoffDelay(attempt, undefined, none))).toEqual([
      1000, 2000, 4000, 8000, 16000, 30000, 30000,
    ]);
    expect(backoffDelay(0, undefined, () => 1)).toBe(800);
    expect(backoffDelay(10, undefined, () => 0.5)).toBe(27000);
  });
});

describe("startEventStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("invalidates mapped queries for named and unnamed events", () => {
    const { stream, invalidated, current } = setup();
    expect(current().url).toBe("/api/v1/events");
    current().open();
    current().emit("document.updated");
    expect(invalidated).toEqual([["documents"], ["review"], ["transactions"], ["accounts"], ["dashboard"]]);
    invalidated.length = 0;
    current().emit("message", JSON.stringify({ id: "x", type: "job.updated" }));
    expect(invalidated).toEqual([["jobs"]]);
    invalidated.length = 0;
    current().emit("message", JSON.stringify({ type: "nobody.cares" }));
    expect(invalidated).toEqual([]);
    stream.close();
  });

  it("reconnects with backoff and refetches everything after reconnecting", () => {
    const { stream, invalidated, statuses, current } = setup();
    const first = current();
    first.open();
    first.fail();
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(1);

    vi.advanceTimersByTime(999);
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(2);

    // A second failure before opening waits twice as long.
    current().fail();
    vi.advanceTimersByTime(1999);
    expect(FakeEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(3);

    current().open();
    expect(invalidated).toHaveLength(allMappedKeys().length);
    expect(statuses).toEqual(["connecting", "open", "reconnecting", "reconnecting", "reconnecting", "reconnecting", "open"]);

    // Backoff resets after a successful open.
    current().fail();
    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(4);
    stream.close();
  });

  it("stops for good on close, including a pending reconnect", () => {
    const { stream, statuses, current } = setup();
    current().fail();
    stream.close();
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(statuses.at(-1)).toBe("closed");
  });
});
