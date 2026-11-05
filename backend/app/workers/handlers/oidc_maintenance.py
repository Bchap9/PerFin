# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC housekeeping jobs (AUTH.md "OIDC"; SECURITY.md B1, B8).

* ``oidc_prune`` (hourly): ``oidc_session`` rows whose session ended (their
  encrypted tokens go with them) and consumed ``state`` hashes past their
  cookie lifetime.
* ``oidc_claim_refresh`` (every 5 minutes): the optional claim refresh with
  the refresh token (``app.auth.oidc.refresh``); a no-op unless OIDC is
  configured and ``PERFIN_OIDC_CLAIM_REFRESH_MINUTES`` > 0. The worker
  reaches the IdP like the API does (``app.core.http``, through
  ``HTTPS_PROXY`` with the egress profile).

Neither table is household-scoped; role changes set their own context.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.auth.oidc.client import OidcClient
from app.auth.oidc.logout import prune
from app.auth.oidc.refresh import refresh_claims
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.workers.registry import JobContext, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

PRUNE_INTERVAL: Final = timedelta(hours=1)
REFRESH_INTERVAL: Final = timedelta(minutes=5)


class OidcPrunePayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OidcClaimRefreshPayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@job_handler("oidc_prune", payload=OidcPrunePayload, timeout=120, max_attempts=3)
def oidc_prune(ctx: JobContext, payload: OidcPrunePayload) -> None:
    ctx.check_cancelled()
    sessions, states = prune(ctx.session)
    log.info("oidc bookkeeping pruned", extra={"oidc_sessions": sessions, "oidc_states": states})


@job_handler("oidc_claim_refresh", payload=OidcClaimRefreshPayload, timeout=240, max_attempts=1)
def oidc_claim_refresh(ctx: JobContext, payload: OidcClaimRefreshPayload) -> None:
    settings = get_settings()
    if not settings.oidc_enabled or settings.oidc_claim_refresh_minutes <= 0:
        return
    ctx.check_cancelled()
    client = OidcClient(settings)
    try:
        stats = refresh_claims(ctx.session, client, Keyring.from_settings(settings), settings)
    finally:
        client.close()
    log.info(
        "oidc claims refreshed",
        extra={"refreshed": stats.refreshed, "revoked": stats.revoked, "skipped": stats.skipped},
    )


recurring("oidc_prune", PRUNE_INTERVAL, OidcPrunePayload(), name="oidc.prune")
recurring(
    "oidc_claim_refresh", REFRESH_INTERVAL, OidcClaimRefreshPayload(), name="oidc.claim_refresh"
)
