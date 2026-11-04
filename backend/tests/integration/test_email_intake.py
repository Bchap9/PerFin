# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mailbox poller against a fake IMAPS server and the real database (D1-D5, C10).

* an accepted mail becomes an ``email`` ``.eml`` document of the member its
  sender maps to, with provenance in ``source_meta``; it stays in the inbox
  until the run committed, then the follow-up run moves it to
  ``PerFin/Processed``; through the real sandbox its attachment becomes a
  child document;
* a spoofed sender, a missing alias, failing DKIM: rejected, the body never
  fetched, moved to ``PerFin/Rejected``, summarised in the digest;
* a flood of rejected mail is **one** ``rejected_email`` review item for the
  day, across runs, with per-run caps;
* a second copy of a message (same ``Message-ID`` and sender) is not
  imported again;
* an extra allowed sender maps to its member; a viewer's address is not
  allowed; after rotating the alias, mail to the old one is rejected;
* credentials are column-encrypted (D2); failures are recorded as outcomes;
* the ``poll_mailbox`` handler: lock, follow-up job, instance switch.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.crypto import DecryptionError, Keyring, column_ad
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentSource,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.intake_settings import service
from app.domain.intake_settings.models import MailIntake, MailIntakeMessage
from app.domain.intake_settings.schemas import AllowedSender, MailIntakeUpdate
from app.ingestion import pipeline
from app.ingestion.intake import email
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig
from app.workers.handlers import mailbox as mailbox_handler
from app.workers.registry import JobContext
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.fake_imap import (
    IMAP_HOST,
    PASSWORD,
    USERNAME,
    FakeImapServer,
    ImapPki,
    make_mail,
    make_pki,
    running_imap,
)

pytestmark = pytest.mark.integration

PROCESSED = "PerFin/Processed"
REJECTED = "PerFin/Rejected"
CSV = b"date,description,amount\n2026-03-14,CORNER MARKET,-12.34\n"


@pytest.fixture
def pki(tmp_path: Path) -> ImapPki:
    return make_pki(tmp_path / "pki")


@pytest.fixture
def imap(pki: ImapPki) -> Iterator[FakeImapServer]:
    with running_imap(pki) as server:
        yield server


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


@pytest.fixture
def keyring(settings: Settings) -> Keyring:
    return Keyring.from_settings(settings)


def _options(pki: ImapPki) -> email.ConnectOptions:
    return email.ConnectOptions(
        proxy=None,
        allow_private=True,
        allow_loopback=True,
        resolver=lambda host, port: [ipaddress.ip_address("127.0.0.1")],
        ssl_context_factory=pki.client_context,
        timeout_seconds=10.0,
    )


class Household:
    """A household with an owner (whose address is ``owner_email``) and a mailbox."""

    def __init__(
        self,
        factories: Factories,
        keyring: Keyring,
        store: LocalFSBlobStore,
        pki: ImapPki,
        server: FakeImapServer,
    ) -> None:
        self.factories = factories
        self.keyring = keyring
        self.store = store
        self.pki = pki
        self.server = server
        tag = uuid.uuid4().hex[:8]
        self.owner_email = f"alex-{tag}@home.invalid"
        self.owner: UserRecord = factories.user(role="owner", email=self.owner_email)
        assert self.owner.household_id is not None
        self.id: uuid.UUID = self.owner.household_id
        self.patch(
            host=IMAP_HOST,
            port=server.port,
            username=USERNAME,
            password=PASSWORD,
            enabled=True,
        )

    def principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.id,
            role=HouseholdRole.OWNER,
            auth_method="session",
        )

    def patch(self, **fields: Any) -> None:
        with self.factories.transaction() as db:
            set_context(db, self.id, self.owner.user.id)
            service.update_intake(
                db,
                self.principal(),
                MailIntakeUpdate.model_validate(fields),
                self.keyring,
                instance_enabled=True,
            )

    def rotate(self) -> str:
        with self.factories.transaction() as db:
            set_context(db, self.id, self.owner.user.id)
            return service.rotate_alias(db, self.principal(), self.keyring).alias_token

    @property
    def alias(self) -> str:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            row = service.get_row(db, self.id)
            assert row is not None
            return service.alias_token(self.keyring, row)

    def to(self, token: str | None = None) -> str:
        return f"receipts+{token or self.alias}@perfin.test"

    def deliver(self, **kwargs: Any) -> int:
        kwargs.setdefault("sender", self.owner_email)
        kwargs.setdefault("to", self.to())
        return self.server.deliver(make_mail(**kwargs))

    def poll(self, *, limits: email.RunLimits | None = None) -> email.PollStats | None:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            return mailbox_handler.poll_household(
                db,
                self.id,
                deps=email.IntakeDeps(store=self.store, keyring=self.keyring),
                options=_options(self.pki),
                limits=limits or email.RunLimits(),
            )

    def rows(self, model: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            found = list(db.execute(select(model).order_by(model.created_at)).scalars())
            db.expunge_all()
            return found

    def digests(self) -> list[ReviewItem]:
        return [r for r in self.rows(ReviewItem) if r.kind is ReviewKind.REJECTED_EMAIL]

    def intake(self) -> MailIntake:
        rows = self.rows(MailIntake)
        assert len(rows) == 1
        row: MailIntake = rows[0]
        return row


@pytest.fixture
def household(
    factories: Factories,
    keyring: Keyring,
    store: LocalFSBlobStore,
    pki: ImapPki,
    imap: FakeImapServer,
) -> Household:
    return Household(factories, keyring, store, pki, imap)


# ---------------------------------------------------------------------------
# Accepted mail
# ---------------------------------------------------------------------------


def test_accepted_mail_becomes_an_email_document_then_moves(household: Household) -> None:
    server = household.server
    household.deliver(
        message_id="<r-accept@shop.invalid>",
        auth_results=[
            "mx.perfin.test; spf=pass smtp.mailfrom=home.invalid; dkim=pass header.d=home.invalid"
        ],
        attachments=[("receipt.csv", "text/csv", CSV)],
    )
    stats = household.poll()
    assert stats is not None
    assert (stats.accepted, stats.rejected) == (1, 0)
    # Two-phase: nothing moved before the run committed.
    assert server.count("INBOX") == 1
    assert server.count(PROCESSED) == 0

    (document,) = household.rows(Document)
    assert document.source is DocumentSource.EMAIL
    assert document.mime_detected == "message/rfc822"
    assert document.uploaded_by == household.owner.user.id
    assert document.original_filename is not None
    assert document.original_filename.endswith(".eml")
    assert document.source_meta == {
        "sender": household.owner_email,
        "message_id": "<r-accept@shop.invalid>",
        "subject": "Your receipt from Corner Market",
        "auth": {"present": True, "spf": "pass", "dkim": "pass"},
    }
    (record,) = household.rows(MailIntakeMessage)
    assert (record.outcome, record.document_id) == ("accepted", document.id)

    again = household.poll()
    assert again is not None
    assert (again.moved, again.accepted) == (1, 0)
    assert server.count("INBOX") == 0
    assert server.count(PROCESSED) == 1
    assert len(household.rows(Document)) == 1
    assert household.intake().last_poll_outcome == "ok"


def test_attachment_goes_through_the_same_intake(household: Household) -> None:
    household.deliver(
        message_id="<r-attach@shop.invalid>", attachments=[("receipt.csv", "text/csv", CSV)]
    )
    household.poll()
    (mail,) = household.rows(Document)
    deps = pipeline.PipelineDeps(
        store=household.store,
        keyring=household.keyring,
        sandbox=SandboxConfig(timeout_seconds=90),
        options=ExtractOptions(preview_dpi=72),
        child_max_bytes=1024 * 1024,
    )
    with household.factories.transaction() as db:
        set_context(db, household.id)
        pipeline.ingest_document(db, household.id, mail.id, deps)
    documents = household.rows(Document)
    children = [d for d in documents if d.id != mail.id]
    assert len(children) == 1
    child = children[0]
    assert child.source is DocumentSource.EMAIL
    assert child.uploaded_by == household.owner.user.id
    assert child.source_meta["parent_document_id"] == str(mail.id)
    assert child.source_meta["depth"] == 1
    assert child.mime_detected == "text/csv"


def test_message_id_dedupe(household: Household) -> None:
    household.deliver(message_id="<r-dup@shop.invalid>")
    household.deliver(message_id="<r-dup@shop.invalid>", subject="Same message again")
    household.poll()
    assert len(household.rows(Document)) == 1
    household.poll()
    # Both copies moved; one document.
    assert household.server.count("INBOX") == 0
    assert household.server.count(PROCESSED) == 2
    household.deliver(message_id="<r-dup@shop.invalid>", subject="A third copy, later")
    stats = household.poll()
    assert stats is not None
    assert (stats.accepted, stats.moved) == (0, 1)
    assert len(household.rows(Document)) == 1


def test_extra_allowed_sender_maps_to_its_member(household: Household) -> None:
    sam = household.factories.user(role=None)
    household.factories.member(household.id, sam.user.id, HouseholdRole.MEMBER)
    household.patch(
        allowed_senders=[AllowedSender(address="Shop@Receipts.invalid", user_id=sam.user.id)]
    )
    household.deliver(sender="Corner Market <shop@receipts.invalid>", message_id="<x1@r.invalid>")
    household.poll()
    (document,) = household.rows(Document)
    assert document.uploaded_by == sam.user.id


def test_viewer_address_is_not_an_allowed_sender(household: Household) -> None:
    address = f"viewer-{uuid.uuid4().hex[:6]}@home.invalid"
    viewer = household.factories.user(role=None, email=address)
    household.factories.member(household.id, viewer.user.id, HouseholdRole.VIEWER)
    household.deliver(sender=address, message_id="<v1@r.invalid>")
    stats = household.poll()
    assert stats is not None
    assert stats.reasons == {"sender_not_allowed": 1}
    assert household.rows(Document) == []


# ---------------------------------------------------------------------------
# Rejected mail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mail", "reason"),
    [
        pytest.param(
            {"sender": "Alex <alex@home.invalid.attacker.invalid>"},
            "sender_not_allowed",
            id="spoofed-sender",
        ),
        pytest.param({"to": "receipts@perfin.test"}, "no_alias", id="missing-alias"),
        pytest.param(
            {"auth_results": ["mx.perfin.test; spf=pass; dkim=fail header.d=home.invalid"]},
            "auth_failed",
            id="failing-dkim",
        ),
    ],
)
def test_rejected_mail_is_moved_and_digested(
    household: Household, mail: dict[str, Any], reason: str
) -> None:
    server = household.server
    household.deliver(**mail)
    stats = household.poll()
    assert stats is not None
    assert stats.reasons == {reason: 1}
    assert household.rows(Document) == []
    # The body of a rejected message is never fetched.
    assert server.body_fetches() == 0
    (digest,) = household.digests()
    assert digest.document_id is None
    assert digest.state is ReviewItemState.OPEN
    assert digest.payload["count"] == 1
    assert digest.payload["reasons"] == {reason: 1}
    assert digest.payload["messages"][0]["reason"] == reason
    assert digest.payload["day"] == datetime.now(UTC).date().isoformat()
    household.poll()
    assert server.count("INBOX") == 0
    assert server.count(REJECTED) == 1
    assert server.count(PROCESSED) == 0


def test_flood_produces_one_digest(household: Household) -> None:
    for i in range(30):
        household.deliver(to=f"receipts+guess{i:04d}@perfin.test", message_id=f"<f{i}@x.invalid>")
    limits = email.RunLimits(max_messages=10)
    first = household.poll(limits=limits)
    assert first is not None
    assert first.rejected == 10
    for _ in range(4):
        household.poll(limits=limits)
    assert household.server.count("INBOX") == 0
    assert household.server.count(REJECTED) == 30
    (digest,) = household.digests()
    assert digest.payload["count"] == 30
    assert digest.payload["reasons"] == {"no_alias": 30}
    assert len(digest.payload["messages"]) == 30
    assert len(household.rows(MailIntakeMessage)) == 30


def test_digest_samples_are_capped(household: Household) -> None:
    for i in range(email.DIGEST_SAMPLES + 5):
        household.deliver(to="receipts@perfin.test", message_id=f"<c{i}@x.invalid>")
    household.poll(limits=email.RunLimits(max_messages=100))
    (digest,) = household.digests()
    assert digest.payload["count"] == email.DIGEST_SAMPLES + 5
    assert len(digest.payload["messages"]) == email.DIGEST_SAMPLES
    assert digest.payload["omitted"] == 5


def test_dismissed_digest_starts_a_new_one(household: Household) -> None:
    household.deliver(to="receipts@perfin.test", message_id="<d1@x.invalid>")
    household.poll()
    (digest,) = household.digests()
    with household.factories.transaction() as db:
        set_context(db, household.id)
        row = db.get(ReviewItem, digest.id)
        assert row is not None
        row.state = ReviewItemState.DISMISSED
        row.resolved_at = datetime.now(UTC)
    household.deliver(to="receipts@perfin.test", message_id="<d2@x.invalid>")
    household.poll()
    open_items = [d for d in household.digests() if d.state is ReviewItemState.OPEN]
    assert len(open_items) == 1
    assert open_items[0].payload["count"] == 1


def test_too_large_mail_is_rejected_without_fetching(household: Household) -> None:
    household.deliver(message_id="<big@x.invalid>", body="x" * 50_000)
    stats = household.poll(limits=email.RunLimits(max_message_bytes=10_000))
    assert stats is not None
    assert stats.reasons == {"too_large": 1}
    assert household.server.body_fetches() == 0


def test_rotated_alias_rejects_the_old_one(household: Household) -> None:
    old = household.alias
    new = household.rotate()
    assert new != old
    assert len(new) == 16
    household.deliver(to=household.to(old), message_id="<old@x.invalid>")
    household.deliver(to=household.to(new), message_id="<new@x.invalid>")
    stats = household.poll()
    assert stats is not None
    assert (stats.accepted, stats.reasons) == (1, {"no_alias": 1})


# ---------------------------------------------------------------------------
# Credentials, failures, handler
# ---------------------------------------------------------------------------


def test_credentials_are_column_encrypted(household: Household) -> None:
    row = household.intake()
    for column in ("username_encrypted", "password_encrypted", "alias_encrypted"):
        value = getattr(row, column)
        assert isinstance(value, bytes)
        assert PASSWORD.encode() not in value
        assert USERNAME.encode() not in value
    secret = row.password_encrypted
    assert secret is not None
    assert (
        household.keyring.decrypt_str(
            secret, ad=column_ad("mail_intake", "password_encrypted", row.id)
        )
        == PASSWORD
    )
    # Bound to column and row: the password ciphertext does not open as the username.
    with pytest.raises(DecryptionError):
        household.keyring.decrypt_str(
            secret, ad=column_ad("mail_intake", "username_encrypted", row.id)
        )
    with pytest.raises(DecryptionError):
        household.keyring.decrypt_str(
            secret, ad=column_ad("mail_intake", "password_encrypted", uuid.uuid4())
        )


def test_wrong_password_is_recorded(household: Household) -> None:
    household.patch(password="wrong")
    assert household.poll() is None
    assert household.intake().last_poll_outcome == "auth_failed"


def test_disabled_mailbox_is_not_polled(household: Household) -> None:
    household.patch(enabled=False)
    household.deliver(message_id="<off@x.invalid>")
    assert household.poll() is None
    assert household.server.commands == []


def test_server_without_move_uses_copy_and_expunge(
    factories: Factories, keyring: Keyring, store: LocalFSBlobStore, pki: ImapPki
) -> None:
    with running_imap(pki, move=False) as server:
        household = Household(factories, keyring, store, pki, server)
        household.deliver(message_id="<nomove@x.invalid>")
        household.poll()
        household.poll()
        assert server.count("INBOX") == 0
        assert server.count(PROCESSED) == 1
        assert not any(" MOVE " in c for c in server.commands)


def _run_handler(household: Household, job_type: str) -> None:
    with household.factories.transaction() as db:
        set_context(db, household.id)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type=job_type,
            attempt=1,
            max_attempts=1,
            household_id=household.id,
            session=db,
            deadline=datetime.max.replace(tzinfo=UTC),
        )
        mailbox_handler.poll_mailbox(ctx, mailbox_handler.PollMailboxPayload())


def _queued_polls(household: Household) -> int:
    return sum(1 for j in household.rows(Job) if j.type == "poll_mailbox")


def test_handler_enqueues_a_follow_up_that_moves(
    household: Household, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = settings.model_copy(update={"email_intake_enabled": True})
    monkeypatch.setattr(mailbox_handler, "get_settings", lambda: enabled)
    monkeypatch.setattr(
        mailbox_handler,
        "intake_deps",
        lambda s: email.IntakeDeps(store=household.store, keyring=household.keyring),
    )
    monkeypatch.setattr(mailbox_handler, "connect_options", lambda s: _options(household.pki))
    household.deliver(message_id="<h1@x.invalid>")
    _run_handler(household, "poll_mailbox")
    assert _queued_polls(household) == 1
    _run_handler(household, "poll_mailbox")
    assert household.server.count(PROCESSED) == 1
    # Nothing new recorded: no further follow-up.
    assert _queued_polls(household) == 1


def test_handler_does_nothing_while_instance_switch_is_off(
    household: Household, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled = settings.model_copy(update={"email_intake_enabled": False})
    monkeypatch.setattr(mailbox_handler, "get_settings", lambda: disabled)
    household.deliver(message_id="<h2@x.invalid>")
    _run_handler(household, "poll_mailbox")
    assert household.server.commands == []


def test_concurrent_run_is_skipped(household: Household) -> None:
    household.deliver(message_id="<lock@x.invalid>")
    with household.factories.transaction() as db:
        set_context(db, household.id)
        assert mailbox_handler.try_lock(db, household.id)
        assert household.poll() is None
    assert household.server.commands == []


def test_dispatcher_enqueues_enabled_households(
    household: Household, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled = settings.model_copy(update={"email_intake_enabled": True})
    monkeypatch.setattr(mailbox_handler, "get_settings", lambda: enabled)
    other = household.factories.user(role="owner")
    with household.factories.transaction() as db:
        set_context(db, None)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type="poll_mailboxes",
            attempt=1,
            max_attempts=1,
            household_id=None,
            session=db,
            deadline=datetime.max.replace(tzinfo=UTC),
        )
        mailbox_handler.poll_mailboxes(ctx, mailbox_handler.PollMailboxesPayload())
    assert _queued_polls(household) == 1
    with household.factories.transaction() as db:
        set_context(db, other.household_id)
        assert not [j for j in db.execute(select(Job)).scalars() if j.type == "poll_mailbox"]
