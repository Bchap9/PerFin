# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channels end to end (API.md, docs/security/notifications.md; A1, A8, I2).

* ``/settings/notifications/channels``: owner/admin browser session, changes
  behind step-up; credentials write-only and encrypted at rest (bound to
  the row); the webhook secret shown once; a destination change needs the
  credentials again; the instance host allowlist; audit without secrets;
* the test send reaches a local fake HTTPS service and records the outcome;
* routing: account lock (A1), recovery code used and MFA reset (A8), role
  changes (I2), sign-in from a new device, an AI client created, tax
  reminders, review items waiting and ``perfin notify backup-failed``
  each queue one ``send_notification`` per subscribed, enabled channel and
  nothing for others; the job payload holds no identity;
* ``send_notification``: delivers through the real adapter, retries
  retryable outcomes, records the final one, skips disabled channels, and
  what leaves the host names nobody and no amount.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.auth import local
from app.auth.local import LockNotice
from app.auth.mfa.stepup import RECOVERY_USED_EVENT, notify_households
from app.core.audit import RequestMeta
from app.core.crypto import DecryptionError, column_ad
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.notifications.backup import notify_backup_failed
from app.domain.notifications.routing import KINDS, SEND_JOB_TYPE, SEND_MAX_ATTEMPTS
from app.domain.tax.reminders import send_reminders
from app.workers.handlers import notifications as notify_jobs
from app.workers.registry import REGISTRY, JobContext
from tests.fixtures.ai import ai_client
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import Factories
from tests.fixtures.fake_imap import ImapPki, make_pki
from tests.fixtures.fake_notify import WEB_HOST, FakeHttpsServer, local_options, running_https
from tests.fixtures.households import API, Actors, household_audit, problem_type
from tests.fixtures.notifications import make_channel
from tests.fixtures.tax import make_checklist_item, make_tax_year

pytestmark = pytest.mark.integration

URL = f"{API}/settings/notifications/channels"
NTFY_TOKEN = "tk_synthetic_ntfy_token"


@pytest.fixture
def pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "pki", WEB_HOST)


@pytest.fixture
def https(pki: ImapPki) -> Iterator[FakeHttpsServer]:
    with running_https(pki) as server:
        yield server


@pytest.fixture
def actors(settings: Settings, factories: Factories, pki: ImapPki) -> Iterator[Actors]:
    with Actors.open(settings, factories) as opened:
        opened.app.state.notification_options = local_options(pki)
        yield opened


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, settings: Settings, pki: ImapPki) -> None:
    monkeypatch.setattr(notify_jobs, "get_settings", lambda: settings)
    monkeypatch.setattr(notify_jobs, "delivery_options", lambda: local_options(pki))


def _jobs(admin_engine: Engine, household_id: uuid.UUID) -> list[dict[str, Any]]:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload FROM job WHERE household_id = :h AND type = :t "
                "ORDER BY created_at, id"
            ),
            {"h": household_id, "t": SEND_JOB_TYPE},
        ).scalars()
        return [dict(r) for r in rows]


def _run_sends(
    factories: Factories, household_id: uuid.UUID, done: set[uuid.UUID], *, attempt: int = 1
) -> list[BaseException | None]:
    """Run the household's queued sends not yet ``done`` once (as the worker would)."""
    spec = REGISTRY.get(SEND_JOB_TYPE)
    assert spec is not None
    with factories.transaction() as db:
        set_context(db, household_id)
        jobs = [
            (j.id, j.payload)
            for j in db.execute(
                select(Job).where(Job.type == SEND_JOB_TYPE).order_by(Job.created_at, Job.id)
            ).scalars()
            if j.id not in done
        ]
    results: list[BaseException | None] = []
    for job_id, payload in jobs:
        try:
            with factories.transaction() as db:
                set_context(db, household_id)
                ctx = JobContext(
                    job_id=job_id,
                    job_type=SEND_JOB_TYPE,
                    attempt=attempt,
                    max_attempts=spec.max_attempts,
                    household_id=household_id,
                    session=db,
                    deadline=datetime.max.replace(tzinfo=UTC),
                )
                spec.func(ctx, spec.validate_payload(payload))
            results.append(None)
            done.add(job_id)
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            results.append(exc)
    return results


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_owner_manages_a_webhook_channel(
    actors: Actors, factories: Factories, admin_engine: Engine, https: FakeHttpsServer
) -> None:
    owner = actors.new(role="owner")
    household_id = owner.record.household_id
    assert household_id is not None
    assert owner.get(URL).json() == []

    created = owner.post(
        URL,
        json={"name": "Home hook", "config": {"type": "webhook", "url": https.url("/hook")}},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["signing_secret"]
    assert isinstance(secret, str)
    assert len(secret) >= 40
    assert body["secrets_set"] == ["secret"]
    assert body["events"] == list(KINDS)
    assert body["config"]["url"] == https.url("/hook")
    listed = owner.get(URL)
    assert secret not in listed.text
    assert "signing_secret" not in listed.json()[0]

    # Encrypted at rest and bound to the row.
    channel_id = uuid.UUID(body["id"])
    with admin_engine.connect() as conn:
        raw = conn.execute(
            text("SELECT config_encrypted FROM notification_channel WHERE id = :i"),
            {"i": channel_id},
        ).scalar_one()
    assert secret.encode() not in bytes(raw)
    assert b"hook" not in bytes(raw)
    keyring = factories.keyring
    assert (
        json.loads(
            keyring.decrypt_str(
                bytes(raw), ad=column_ad("notification_channel", "config_encrypted", channel_id)
            )
        )["secret"]
        == secret
    )
    with pytest.raises(DecryptionError):
        keyring.decrypt_str(
            bytes(raw), ad=column_ad("notification_channel", "config_encrypted", uuid.uuid4())
        )

    test = owner.post(f"{URL}/{channel_id}/test")
    assert test.status_code == 200, test.text
    assert test.json() == {"ok": True, "outcome": "ok"}
    (request,) = https.requests
    assert json.loads(request.body)["kind"] == "test"
    assert owner.get(URL).json()[0]["last_outcome"] == "ok"

    rotated = owner.patch(f"{URL}/{channel_id}", json={"rotate_secret": True, "name": "Hook"})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["signing_secret"] not in (None, secret)
    assert rotated.json()["name"] == "Hook"

    https.statuses.append(500)
    failed = owner.post(f"{URL}/{channel_id}/test").json()
    assert failed == {"ok": False, "outcome": "server_error"}

    audits = [
        *household_audit(factories, household_id, "notification_channel.create"),
        *household_audit(factories, household_id, "notification_channel.update"),
    ]
    assert len(audits) == 2
    dumped = json.dumps([a.after for a in audits])
    assert secret not in dumped
    assert rotated.json()["signing_secret"] not in dumped
    assert "/hook" not in dumped
    assert str(https.port) not in dumped
    assert "config.secret" in dumped

    gone = owner.delete(f"{URL}/{channel_id}")
    assert gone.status_code == 204
    assert owner.get(URL).json() == []
    assert household_audit(factories, household_id, "notification_channel.delete")


def _stored_webhook_secret(admin_engine: Engine, factories: Factories, channel_id: str) -> str:
    row_id = uuid.UUID(channel_id)
    with admin_engine.connect() as conn:
        raw = conn.execute(
            text("SELECT config_encrypted FROM notification_channel WHERE id = :i"),
            {"i": row_id},
        ).scalar_one()
    config = json.loads(
        factories.keyring.decrypt_str(
            bytes(raw), ad=column_ad("notification_channel", "config_encrypted", row_id)
        )
    )
    secret: str = config["secret"]
    return secret


def test_webhook_moved_to_another_host_gets_a_new_signing_secret(
    actors: Actors, factories: Factories, admin_engine: Engine
) -> None:
    """P7-SR-R1-05: bodies signed for one receiver must not verify at another."""
    owner = actors.new(role="owner")
    created = owner.post(
        URL,
        json={"name": "Hook", "config": {"type": "webhook", "url": "https://hooks.example.org/a"}},
    )
    assert created.status_code == 201, created.text
    first = created.json()["signing_secret"]
    channel_id = created.json()["id"]
    channel = f"{URL}/{channel_id}"
    assert _stored_webhook_secret(admin_engine, factories, channel_id) == first

    # Another path on the same receiver: the secret stays and is not shown.
    same_host = owner.patch(channel, json={"config": {"url": "https://hooks.example.org/b"}})
    assert same_host.status_code == 200, same_host.text
    assert same_host.json()["signing_secret"] is None
    assert _stored_webhook_secret(admin_engine, factories, channel_id) == first

    # Another host: a new secret, returned once and stored in place of the old.
    moved = owner.patch(channel, json={"config": {"url": "https://other.example.net/b"}})
    assert moved.status_code == 200, moved.text
    second = moved.json()["signing_secret"]
    assert isinstance(second, str)
    assert second != first
    assert len(second) >= 40
    assert _stored_webhook_secret(admin_engine, factories, channel_id) == second
    assert second not in owner.get(URL).text

    # Another port on the same host is another receiver too.
    port = owner.patch(channel, json={"config": {"url": "https://other.example.net:8443/b"}})
    assert port.status_code == 200, port.text
    third = port.json()["signing_secret"]
    assert third not in (None, first, second)
    assert _stored_webhook_secret(admin_engine, factories, channel_id) == third

    household_id = owner.record.household_id
    assert household_id is not None
    audits = household_audit(factories, household_id, "notification_channel.update")
    dumped = json.dumps([a.after for a in audits])
    assert "config.secret" in dumped
    for value in (first, second, third):
        assert value not in dumped


def test_credentials_are_write_only_and_follow_the_destination(actors: Actors) -> None:
    owner = actors.new(role="owner")
    created = owner.post(
        URL,
        json={
            "name": "Phone",
            "events": ["account_locked", "role_changed"],
            "config": {
                "type": "ntfy",
                "server_url": "https://ntfy.example.org",
                "topic": "perfin-x1",
                "token": NTFY_TOKEN,
            },
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["signing_secret"] is None
    assert sorted(body["secrets_set"]) == ["token", "topic"]
    assert NTFY_TOKEN not in created.text
    assert "perfin-x1" not in created.text
    channel = f"{URL}/{body['id']}"

    # Same destination: the stored token stays.
    same = owner.patch(channel, json={"config": {"priority": 5}})
    assert same.status_code == 200, same.text
    assert same.json()["config"]["priority"] == 5
    assert sorted(same.json()["secrets_set"]) == ["token", "topic"]
    # Another server without the credentials again: refused.
    moved = owner.patch(channel, json={"config": {"server_url": "https://ntfy.evil.example"}})
    assert moved.status_code == 422
    assert problem_type(moved) == "notification-secret-required"
    retyped = owner.patch(
        channel,
        json={
            "config": {
                "server_url": "https://ntfy2.example.org",
                "topic": "perfin-x2",
                "token": "tk_new",
            }
        },
    )
    assert retyped.status_code == 200, retyped.text
    assert retyped.json()["config"]["server_url"] == "https://ntfy2.example.org"
    # The type cannot change; unknown kinds and bad URLs are refused.
    assert (
        problem_type(owner.patch(channel, json={"config": {"type": "gotify"}}))
        == "notification-type-immutable"
    )
    assert owner.patch(channel, json={"events": ["everything"]}).status_code == 422
    assert (
        owner.patch(channel, json={"config": {"server_url": "http://ntfy.example.org"}}).status_code
        == 422
    )
    assert (
        problem_type(owner.patch(channel, json={"rotate_secret": True}))
        == "notification-no-signing-secret"
    )
    assert owner.patch(channel, json={"events": [], "enabled": False}).json()["enabled"] is False
    assert owner.patch(f"{URL}/{uuid.uuid4()}", json={}).status_code == 404


def test_instance_allowlist_is_enforced(actors: Actors, pki: ImapPki) -> None:
    actors.app.state.notification_options = local_options(pki, allowed_hosts=("hooks.example.org",))
    owner = actors.new(role="owner")
    refused = owner.post(
        URL, json={"name": "x", "config": {"type": "webhook", "url": "https://other.example.org/h"}}
    )
    assert refused.status_code == 422
    assert problem_type(refused) == "notification-host-not-allowed"
    allowed = owner.post(
        URL, json={"name": "x", "config": {"type": "webhook", "url": "https://hooks.example.org/h"}}
    )
    assert allowed.status_code == 201, allowed.text
    moved = owner.patch(
        f"{URL}/{allowed.json()['id']}", json={"config": {"url": "https://other.example.org/h"}}
    )
    assert problem_type(moved) == "notification-host-not-allowed"


def test_step_up_and_roles(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    stale = actors.new(role="admin", household=household, mfa_verified=False)
    member = actors.new(role="member", household=household)
    with factories.transaction() as db:
        row = make_channel(db, household.id, factories.keyring)
    channel = f"{URL}/{row.id}"
    assert stale.get(URL).status_code == 200
    for response in (
        stale.post(
            URL, json={"name": "x", "config": {"type": "webhook", "url": "https://h.example.org"}}
        ),
        stale.patch(channel, json={"enabled": False}),
        stale.delete(channel),
    ):
        assert response.status_code == 403
        assert problem_type(response) == "step-up-required"
    for response in (member.get(URL), member.post(f"{channel}/test"), member.delete(channel)):
        assert response.status_code == 403
    # Another household's channel does not exist for this owner.
    other = actors.new(role="owner")
    assert other.post(f"{channel}/test").status_code == 404
    assert other.delete(channel).status_code == 404


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _three_channels(factories: Factories, household_id: uuid.UUID, kind: str) -> uuid.UUID:
    """A subscribed channel (returned), an unsubscribed one and a disabled one."""
    with factories.transaction() as db:
        wanted = make_channel(db, household_id, factories.keyring, events=[kind])
        make_channel(db, household_id, factories.keyring, events=[k for k in KINDS if k != kind])
        make_channel(db, household_id, factories.keyring, events=list(KINDS), enabled=False)
    return wanted.id


def _assert_routed(
    admin_engine: Engine, household_id: uuid.UUID, channel_id: uuid.UUID, event_type: str
) -> None:
    jobs = [j for j in _jobs(admin_engine, household_id) if j["event_type"] == event_type]
    assert len(jobs) == 1, jobs
    (payload,) = jobs
    assert set(payload) == {"channel_id", "event_type", "delivery_id"}
    assert payload["channel_id"] == str(channel_id)
    assert payload["event_type"] == event_type


def test_account_lock_is_routed(factories: Factories, admin_engine: Engine) -> None:
    record = factories.user(role="member")
    assert record.household_id is not None
    channel = _three_channels(factories, record.household_id, "account_locked")
    local.notify_account_locked(
        factories.session_factory, LockNotice(record.id, None, "192.0.2.1"), RequestMeta()
    )
    _assert_routed(admin_engine, record.household_id, channel, "user.locked")


def test_recovery_code_and_mfa_reset_are_routed(factories: Factories, admin_engine: Engine) -> None:
    record = factories.user(role="member")
    household_id = record.household_id
    assert household_id is not None
    channel = _three_channels(factories, household_id, "recovery_code_used")
    with factories.transaction() as db:
        notify_households(db, record.id, RECOVERY_USED_EVENT, {"user_id": str(record.id)})
        notify_households(db, record.id, "user.mfa_reset", {"user_id": str(record.id)})
    _assert_routed(admin_engine, household_id, channel, RECOVERY_USED_EVENT)
    # user.mfa_reset went to the channel subscribed to everything but recovery codes.
    (reset,) = [j for j in _jobs(admin_engine, household_id) if j["event_type"] == "user.mfa_reset"]
    assert reset["channel_id"] != str(channel)


def test_role_change_through_the_api_is_routed(
    actors: Actors, factories: Factories, admin_engine: Engine
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    channel = _three_channels(factories, household.id, "role_changed")
    target = factories.user(role="member", household=household)
    changed = owner.patch(
        f"{API}/households/{household.id}/members/{target.id}", json={"role": "admin"}
    )
    assert changed.status_code == 200, changed.text
    _assert_routed(admin_engine, household.id, channel, "member.role_changed")


def test_ai_client_creation_is_routed(factories: Factories, admin_engine: Engine) -> None:
    owner = factories.user(role="owner", with_totp=True)
    assert owner.household_id is not None
    channel = _three_channels(factories, owner.household_id, "ai_client_created")
    ai_client(factories, owner)
    _assert_routed(admin_engine, owner.household_id, channel, "ai_client.created")


def test_tax_reminder_is_routed(factories: Factories, admin_engine: Engine) -> None:
    record = factories.user(role="owner")
    household_id = record.household_id
    assert household_id is not None
    channel = _three_channels(factories, household_id, "tax_reminder")
    with factories.transaction() as db:
        year = make_tax_year(db, household_id, year=2025)
        make_checklist_item(db, year)
    with factories.transaction() as db:
        set_context(db, household_id)
        sent = send_reminders(db, household_id, date(2026, 1, 31), "01-31")
    assert len(sent) == 1
    _assert_routed(admin_engine, household_id, channel, "tax.reminder")


def test_new_device_sign_in_is_routed(
    settings: Settings, factories: Factories, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    assert record.household_id is not None
    channel = _three_channels(factories, record.household_id, "new_device")
    app = build_app(settings)

    def sign_in(agent: str) -> None:
        with browser(app) as client:
            headers = {**pre_auth_headers(client), "User-Agent": agent}
            response = client.post(
                f"{API}/auth/login",
                json={"username": record.user.username, "password": record.password},
                headers=headers,
            )
            assert response.status_code == 200, response.text

    sign_in("Synthetic Browser 1.0")  # the first sign-in ever: nothing to compare with
    assert _jobs(admin_engine, record.household_id) == []
    sign_in("Synthetic Browser 1.0")  # same browser again
    assert _jobs(admin_engine, record.household_id) == []
    sign_in("Other Browser 2.0")
    _assert_routed(admin_engine, record.household_id, channel, "security.new_device")


def test_review_waiting_and_backup_failed_are_routed(
    factories: Factories, admin_engine: Engine, tmp_path: Path
) -> None:
    from app.domain.documents.blobstore import LocalFSBlobStore

    waiting = factories.user(role="owner")
    idle = factories.user(role="owner")
    assert waiting.household_id is not None
    assert idle.household_id is not None
    review_channel = _three_channels(factories, waiting.household_id, "review_waiting")
    with factories.transaction() as db:
        make_channel(db, idle.household_id, factories.keyring, events=["review_waiting"])
        store = LocalFSBlobStore(tmp_path / "blobs", tmp_path / "quarantine")
        document = make_document(db, store, factories.keyring, waiting.household_id)
        make_review_item(db, document)
    spec = REGISTRY.get("notify_review_waiting")
    assert spec is not None
    for _ in range(2):  # the same day twice: one notification
        with factories.transaction() as db:
            ctx = JobContext(
                job_id=uuid.uuid4(),
                job_type="notify_review_waiting",
                attempt=1,
                max_attempts=3,
                household_id=None,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            )
            spec.func(ctx, spec.validate_payload({}))
    _assert_routed(admin_engine, waiting.household_id, review_channel, "review.waiting")
    assert _jobs(admin_engine, idle.household_id) == []  # nothing waiting there

    with factories.transaction() as db:
        backup_channel = make_channel(
            db, idle.household_id, factories.keyring, events=["backup_failed"]
        )
    with factories.transaction() as db:
        assert notify_backup_failed(db) >= 1
    _assert_routed(admin_engine, idle.household_id, backup_channel.id, "backup.failed")


# ---------------------------------------------------------------------------
# Delivery job
# ---------------------------------------------------------------------------


def test_send_job_delivers_retries_and_records(
    actors: Actors,
    factories: Factories,
    admin_engine: Engine,
    https: FakeHttpsServer,
    worker: None,
) -> None:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    created = owner.post(
        URL,
        json={
            "name": "Hook",
            "events": ["role_changed"],
            "config": {"type": "webhook", "url": https.url("/hook")},
        },
    ).json()
    target = factories.user(role="member", household=household)
    owner.patch(f"{API}/households/{household.id}/members/{target.id}", json={"role": "viewer"})

    done: set[uuid.UUID] = set()
    https.statuses.append(503)
    (first,) = _run_sends(factories, household.id, done, attempt=1)
    assert isinstance(first, notify_jobs.DeliveryRetryError)
    assert owner.get(URL).json()[0]["last_outcome"] is None  # rolled back with the attempt
    (second,) = _run_sends(factories, household.id, done, attempt=2)
    assert second is None
    channel = owner.get(URL).json()[0]
    assert channel["last_outcome"] == "ok"
    assert channel["last_sent_at"] is not None

    assert len(https.requests) == 2
    delivered = https.requests[-1]
    body = json.loads(delivered.body)
    assert body["kind"] == "role_changed"
    assert body["message"] == "A household member's role was changed."
    leaked = delivered.body.decode() + json.dumps(delivered.headers)
    for identity in (
        target.user.username,
        *([target.user.email] if target.user.email else []),
        owner.record.user.username,
        household.name,
        str(target.id),
        str(household.id),
    ):
        assert identity not in leaked
    # The last attempt records a retryable failure instead of retrying again.
    owner.patch(f"{API}/households/{household.id}/members/{target.id}", json={"role": "member"})
    https.statuses.append(503)
    (last,) = _run_sends(factories, household.id, done, attempt=SEND_MAX_ATTEMPTS)
    assert last is None
    assert owner.get(URL).json()[0]["last_outcome"] == "server_error"

    # Queued while enabled, disabled before it runs: nothing is sent.
    owner.patch(f"{API}/households/{household.id}/members/{target.id}", json={"role": "viewer"})
    owner.patch(f"{URL}/{created['id']}", json={"enabled": False})
    before = len(https.requests)
    assert _run_sends(factories, household.id, done, attempt=1) == [None]
    assert len(https.requests) == before
