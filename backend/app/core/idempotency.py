# SPDX-License-Identifier: AGPL-3.0-or-later
"""``Idempotency-Key`` for creating ``POST`` endpoints (API.md "Conventions").

A client that may retry a creating request (a flaky network, a double
click) sends ``Idempotency-Key: <1-255 visible ASCII characters>``. The first
request with a key runs and its answer is stored; a later request with the
same key, by the same user in the same household, gets the stored answer
(same status and body, plus ``Idempotent-Replayed: true``) without running
again. Reusing a key for a *different* request (another route, other path
parameters or another body) is ``422 idempotency-key-reused``. Without the
header the endpoint behaves as usual.

Usage in a router (the guard dependencies have already run)::

    @router.post("/things", response_model=ThingOut, status_code=201)
    def post_thing(body: ThingCreate, db: DbSession, principal: Writer,
                   idempotency_key: IdempotencyKeyHeader = None) -> Any:
        return idempotent(
            db, principal, idempotency_key,
            scope="POST /things", payload=body, status_code=201,
            run=lambda: service.create_thing(db, principal, body),
        )

How it holds under retries and concurrency:

* The key row (``app.db.models.IdempotencyKey``) is inserted **before** the
  work and completed after it, in the request's own transaction. A failed
  request (any error, including a problem response) rolls the row back with
  the change, so the retry runs again; a successful one commits both.
* A concurrent request with the same key waits on the unique index until
  the first commits (then replays its answer) or rolls back (then runs
  itself). A wait longer than the connection's ``lock_timeout`` is ``409
  idempotency-key-in-use`` with ``Retry-After``.
* Keys are scoped by household **and** user (a token acts as its user), so
  one member can never replay another's response. Rows live
  :data:`KEY_TTL`; expired rows of the household are removed whenever a new
  key is stored, and an expired key may be used again.
* Only successful (2xx) answers are stored. The stored body is the response
  model's JSON, i.e. exactly what the caller was allowed to see.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final

from fastapi import Header
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.errors import ProblemError
from app.db.models import IdempotencyKey

if TYPE_CHECKING:
    import uuid

    from app.auth.principal import Principal

HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"
KEY_MAX: Final = 255
KEY_TTL: Final = timedelta(hours=24)
_KEY_RE: Final = re.compile(r"^[\x21-\x7e]{1,255}$")

#: The ``Idempotency-Key`` request header as a FastAPI parameter (optional).
IdempotencyKeyHeader = Annotated[
    str | None,
    Header(
        alias=HEADER,
        description=(
            "Optional. Repeating a request with the same key returns the first answer "
            "instead of creating again (24 hours; 1-255 visible ASCII characters)."
        ),
    ),
]


def invalid_key() -> ProblemError:
    return ProblemError(
        422,
        "invalid-idempotency-key",
        "Invalid Idempotency-Key",
        f"Use 1 to {KEY_MAX} visible ASCII characters (a UUID is a good choice).",
    )


def key_reused() -> ProblemError:
    return ProblemError(
        422,
        "idempotency-key-reused",
        "Idempotency-Key reused",
        "This key was already used for a different request. Use a new key.",
    )


def key_in_use() -> ProblemError:
    return ProblemError(
        409,
        "idempotency-key-in-use",
        "Request in progress",
        "A request with this Idempotency-Key is still being processed. Retry shortly.",
        headers={"Retry-After": "1"},
    )


def check_key(key: str) -> str:
    """``key`` if it is acceptable, else ``422 invalid-idempotency-key``."""
    if not _KEY_RE.fullmatch(key):
        raise invalid_key()
    return key


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return jsonable_encoder(value)


def request_hash(scope: str, payload: Any) -> str:
    """SHA-256 (hex) over the route ``scope`` and the canonical JSON of ``payload``."""
    document = {"scope": scope, "payload": _jsonable(payload)}
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_response(status_code: int, body: Any) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={REPLAYED_HEADER: "true"})


def idempotent[T: BaseModel](
    db: Session,
    principal: Principal,
    key: str | None,
    *,
    scope: str,
    payload: Any,
    run: Callable[[], T],
    status_code: int = 201,
) -> T | JSONResponse:
    """Run ``run`` at most once per ``key`` (see the module doc).

    ``scope`` names the route (``"POST /accounts/{account_id}/balances"``);
    ``payload`` is everything else that identifies the request (the body
    model, or a mapping of path parameters and body). Returns ``run()``'s
    model on the first call and a :class:`JSONResponse` replay afterwards.
    """
    if key is None:
        return run()
    check_key(key)
    household_id = principal.household_id
    if household_id is None:  # creating routes always run in a household
        raise RuntimeError("idempotency needs a household context")
    digest = request_hash(scope, payload)
    now = clock.utcnow()
    db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.household_id == household_id, IdempotencyKey.expires_at <= now
        )
    )
    statement = (
        insert(IdempotencyKey)
        .values(
            household_id=household_id,
            user_id=principal.user_id,
            key=key,
            request_hash=digest,
            created_at=now,
            expires_at=now + KEY_TTL,
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_key_scope")
        .returning(IdempotencyKey.id)
    )
    try:
        row_id = db.execute(statement).scalar_one_or_none()
    except OperationalError as exc:  # lock_timeout while another request holds the key
        raise key_in_use() from exc
    if row_id is None:
        return _replay(db, household_id, principal, key, digest)
    result = run()
    db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == row_id, IdempotencyKey.household_id == household_id)
        .values(status_code=status_code, response_body=_jsonable(result))
    )
    return result


def _replay(
    db: Session, household_id: uuid.UUID, principal: Principal, key: str, digest: str
) -> JSONResponse:
    stored = db.execute(
        select(
            IdempotencyKey.request_hash,
            IdempotencyKey.status_code,
            IdempotencyKey.response_body,
        ).where(
            IdempotencyKey.household_id == household_id,
            IdempotencyKey.user_id == principal.user_id,
            IdempotencyKey.key == key,
        )
    ).one_or_none()
    if stored is None:  # pragma: no cover - expired and removed between the statements
        raise key_in_use()
    stored_hash, stored_status, stored_body = stored
    if stored_hash != digest:
        raise key_reused()
    if stored_status is None:  # pragma: no cover - only inside the first request's transaction
        raise key_in_use()
    return replay_response(int(stored_status), stored_body)


def payload_of(body: BaseModel | None = None, **path: Any) -> Mapping[str, Any]:
    """A request ``payload`` from path parameters and an optional body model."""
    return {"path": {k: _jsonable(v) for k, v in path.items()}, "body": _jsonable(body)}
