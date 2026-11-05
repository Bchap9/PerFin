# SPDX-License-Identifier: AGPL-3.0-or-later
"""One row in every master-key-encrypted column, plus a document (key rotation, backups).

``populate(factories, store)`` creates a household with a TOTP user, a
browser session with an OIDC side and a pending TOTP enrollment, a
notification channel, a mail intake with credentials, a portability archive
with a wrapped passphrase key and a stored document, all encrypted with
``factories.keyring``. It returns :class:`Populated`: the plaintext expected
in each ``app.ops.keyrotation.ENVELOPE_COLUMNS`` target and the document's
id. Every value is synthetic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from app.auth.mfa.models import MfaChallenge
from app.auth.models import MfaMethod
from app.auth.oidc.models import OidcSession
from app.core import clock
from app.core.crypto import column_ad
from app.core.ids import uuid7
from app.domain.documents.blobstore import BlobStore
from app.domain.intake_settings.models import MailIntake
from app.domain.notifications.models import NotificationChannel
from app.domain.portability.models import ArchiveKind, PortabilityArchive
from app.integrations.notifications.base import ChannelType
from tests.fixtures.documents import make_document
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import household_context


@dataclass
class Populated:
    household_id: uuid.UUID
    user_id: uuid.UUID
    document_id: uuid.UUID
    #: (table, column, row id) -> plaintext
    values: dict[tuple[str, str, uuid.UUID], bytes] = field(default_factory=dict)


def populate(factories: Factories, store: BlobStore, *, tag: str = "a") -> Populated:
    keyring = factories.keyring
    record = factories.user(role="owner", with_totp=True)
    assert record.household_id is not None
    household_id = record.household_id
    issued = factories.session(record)
    out = Populated(household_id, record.id, uuid.UUID(int=0))

    def enc(table: str, column: str, row_id: uuid.UUID, value: bytes) -> bytes:
        out.values[(table, column, row_id)] = value
        return keyring.encrypt_bytes(value, ad=column_ad(table, column, row_id))

    with factories.transaction() as db:
        method = db.query(MfaMethod).filter(MfaMethod.user_id == record.id).one()
        assert method.secret_encrypted is not None
        out.values[("mfa_method", "secret_encrypted", method.id)] = keyring.decrypt_bytes(
            method.secret_encrypted, ad=column_ad("mfa_method", "secret_encrypted", method.id)
        )
        now = clock.utcnow()
        challenge_id = uuid7()
        db.add(
            MfaChallenge(
                id=challenge_id,
                session_id=issued.row.id,
                user_id=record.id,
                purpose="totp_enroll",
                secret_encrypted=enc(
                    "mfa_challenge",
                    "secret_encrypted",
                    challenge_id,
                    b"JBSWY3DPEHPK3PXP" + tag.encode(),
                ),
                expires_at=now + timedelta(minutes=10),
            )
        )
        oidc_id = uuid7()
        db.add(
            OidcSession(
                id=oidc_id,
                session_id=issued.row.id,
                user_id=record.id,
                issuer="https://idp.example.test",
                subject=f"synthetic-{tag}",
                id_token_encrypted=enc(
                    "oidc_session", "id_token_encrypted", oidc_id, b"synthetic.id.token"
                ),
                refresh_token_encrypted=enc(
                    "oidc_session", "refresh_token_encrypted", oidc_id, b"synthetic-refresh"
                ),
            )
        )
        with household_context(db, household_id):
            channel_id = uuid7()
            db.add(
                NotificationChannel(
                    id=channel_id,
                    household_id=household_id,
                    name="Synthetic channel",
                    type=ChannelType.NTFY,
                    config_encrypted=enc(
                        "notification_channel",
                        "config_encrypted",
                        channel_id,
                        b'{"url": "https://ntfy.example.test/topic"}',
                    ),
                    events=[],
                )
            )
            intake_id = uuid7()
            db.add(
                MailIntake(
                    id=intake_id,
                    household_id=household_id,
                    host="imap.example.test",
                    username_encrypted=enc(
                        "mail_intake", "username_encrypted", intake_id, b"receipts@example.test"
                    ),
                    password_encrypted=enc(
                        "mail_intake", "password_encrypted", intake_id, b"synthetic-password"
                    ),
                    alias_encrypted=enc("mail_intake", "alias_encrypted", intake_id, b"alias123"),
                    alias_rotated_at=now,
                    allowed_senders=[],
                    last_poll_counts={},
                )
            )
            archive_id = uuid7()
            db.add(
                PortabilityArchive(
                    id=archive_id,
                    household_id=household_id,
                    kind=ArchiveKind.EXPORT,
                    job_id=uuid7(),
                    passphrase_protected=True,
                    passphrase_key=enc(
                        "portability_archive", "passphrase_key", archive_id, bytes(range(48))
                    ),
                    summary={},
                )
            )
            db.flush()
        document = make_document(
            db,
            store,
            keyring,
            household_id,
            data=f"Date,Payee,Amount\n2026-02-0{len(tag)},Synthetic {tag},-1.00\n".encode(),
        )
        out.document_id = document.id
    return out
