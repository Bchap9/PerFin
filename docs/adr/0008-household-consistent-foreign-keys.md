# ADR-0008: Household-consistent foreign keys between household-scoped tables

Status: Accepted. Date: 2026-11-03.

## Context
Every household-scoped table has forced row-level security, and every query
also filters `household_id` explicitly. Foreign keys, however, are checked by
Postgres without RLS: a plain `transaction.account_id REFERENCES account(id)`
accepts the id of another household's account. A service that forgets to
check an id from a request body (a payee, a category, an account in a bulk
edit) would then link one household's rows to another's. RLS would hide the
foreign row from reads, but the link itself is corrupt data, and a later bug
in a join could expose it (SECURITY.md F3).

## Decision
Every reference from one household-scoped table to another includes
`household_id` (`app.db.types`):

- the target declares `UNIQUE (household_id, id)` (`household_key`);
- a mandatory parent is `FOREIGN KEY (household_id, x_id) REFERENCES
  x(household_id, id)` (`household_fk`), usually `ON DELETE CASCADE`, or
  `NO ACTION` to protect the parent (`category.parent_id`);
- an optional lookup that must be cleared when its target is deleted keeps a
  plain `FOREIGN KEY (x_id) REFERENCES x(id) ON DELETE SET NULL` **and** a
  composite `FOREIGN KEY (household_id, x_id) ... DEFERRABLE INITIALLY
  DEFERRED` (`household_ref_fk`): a composite `SET NULL` would also null
  `household_id`, and the deferred composite check runs at commit, after the
  `SET NULL`, and still refuses another household's id;
- a key may carry more columns where they must agree: `transaction`,
  `balance_snapshot` and `scheduled_transaction` reference
  `account(household_id, id, currency)`, so their currency is the account's.

## Alternatives considered
- **Service checks only.** Every write path must remember; one omission is a
  cross-household link. Kept as well (services still answer `404`), but not
  relied upon.
- **Triggers that look the target up.** Same guarantee, more code, invisible
  to Alembic autogenerate and slower.
- **Postgres 15+ `ON DELETE SET NULL (column)`** on the composite key. Would
  avoid the second key, but SQLAlchemy cannot express it and Alembic
  autogenerate would report drift.

## Consequences
- One extra unique index per referenced table and one extra (deferred) key
  per optional lookup.
- A cross-household id in an optional lookup fails at commit (an
  `IntegrityError` from the commit, not the flush); tests use `SET
  CONSTRAINTS ALL IMMEDIATE` to check it inside a rolled-back transaction.
- An account's currency cannot change while it has transactions, snapshots
  or schedules.
- New household-scoped tables follow the same pattern (DEVELOPMENT.md,
  "Database rules").
