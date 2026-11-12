# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asking for a price refresh now (``POST /investments/prices/refresh``).

The ``refresh_prices`` job (``app.workers.handlers.market_data``) runs on a
schedule in the worker. This module lets the API queue one more run without
importing the handler: the job's type and payload live here.

The job is instance-level like the recurring one: the provider is asked once
per distinct symbol whoever asked, so a request says nothing about the
household that made it. Requests inside the same
:data:`MANUAL_REFRESH_SECONDS` window share one job (its dedupe key), so no
member can make the worker call the provider more often than that. The API
process itself never contacts a feed (docs/security/integrations.md).
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core import clock
from app.core.settings import Settings
from app.db.queue import enqueue

REFRESH_JOB_TYPE: Final = "refresh_prices"
#: A refresh asked for by a person runs at most once per this many seconds,
#: for the whole instance.
MANUAL_REFRESH_SECONDS: Final = 60


class RefreshPricesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def price_feed_enabled(settings: Settings) -> bool:
    """An external price provider is configured (``PERFIN_PRICE_PROVIDER``)."""
    return settings.price_provider != "none"


def request_refresh(db: Session) -> uuid.UUID:
    """Queue one refresh now; the caller has checked :func:`price_feed_enabled`."""
    slot = int(clock.utcnow().timestamp()) // MANUAL_REFRESH_SECONDS
    return enqueue(
        db,
        REFRESH_JOB_TYPE,
        RefreshPricesPayload(),
        dedupe_key=f"manual:{REFRESH_JOB_TYPE}:{slot}",
    )
