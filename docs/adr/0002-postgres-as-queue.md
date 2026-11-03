# ADR-0002: PostgreSQL as the job queue

Status: Accepted. Date: 2026-11-03.

## Context
The worker needs a durable job queue for ingestion, scheduled snapshots, price
refreshes and notifications. Self-hosters pay for every extra service in
attack surface, memory and upgrade effort.

## Decision
Use a `job` table in PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` for
dequeue, `run_after` for scheduling and retries with exponential backoff, a
`locked_by`/`locked_at` lease with reaper for crashed workers, and a
dead-letter state. Job payloads are JSONB validated by Pydantic. Enqueue is
transactional with the business write that caused it.

## Alternatives considered
- **Redis + Celery/arq/Dramatiq.** Mature, but another service to run, secure
  (Redis defaults to no auth) and back up, and the volumes here are tiny
  (dozens of jobs a day, not thousands a second).
- **RabbitMQ / NATS.** Same objection, heavier.
- **In-process background tasks.** No durability; a restart loses work.

## Consequences
- Exactly-once semantics within the database: enqueuing a job in the same
  transaction as the document row means no orphaned jobs.
- Polling interval (default 1 s with `LISTEN/NOTIFY` wake-ups) is more than
  fast enough.
- If throughput ever matters, the `Queue` interface allows swapping in a
  broker without touching job handlers.
