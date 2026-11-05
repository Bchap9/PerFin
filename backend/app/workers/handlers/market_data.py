# SPDX-License-Identifier: AGPL-3.0-or-later
"""``refresh_prices``: the opt-in price and FX feeds (ARCHITECTURE.md "Jobs").

An instance-level recurring job (every :data:`REFRESH_INTERVAL`, one per slot
however many workers run). With neither ``PERFIN_PRICE_PROVIDER`` nor
``PERFIN_FX_PROVIDER`` set (the default) it returns at once and contacts
nothing.

With a price provider it collects the distinct ticker symbols of every
household's securities (each household read in its own RLS context), asks
the provider **once per distinct symbol** for the whole instance (so the
provider cannot tell how many households hold a symbol, or which), then
writes the quotes into each household's ``price`` rows
(``app.integrations.prices.store``; manual prices win). Symbols are the only
household-derived data that leave the host; no quantity, holding, account,
household or user ever does (docs/security/integrations.md).

With an FX provider it fetches the reference rates once and stores, per
household, the rates of the currencies that household uses against its base
currency (``app.integrations.fx.store``; manual rates win). The ECB request
carries nothing household-specific at all.

Failures of one feed do not stop the other; the job is not retried by the
queue (``max_attempts=1``): the next slot tries again. Logs carry counts
only, never symbols.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import clock
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.rls import set_context
from app.integrations.fx.base import FxProvider, FxProviderError, FxQuote
from app.integrations.fx.registry import external_fx_provider
from app.integrations.fx.store import store_reference_rates
from app.integrations.prices.base import PriceProvider, PriceProviderConfigError, PriceQuote
from app.integrations.prices.registry import external_price_provider
from app.integrations.prices.store import household_symbols, store_quotes
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

REFRESH_JOB_TYPE: Final = "refresh_prices"
REFRESH_INTERVAL: Final = timedelta(hours=6)
REFRESH_TIMEOUT_SECONDS: Final = 1800.0


class RefreshPricesPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class RefreshStats:
    households: int = 0
    symbols: int = 0
    quotes: int = 0
    prices_written: int = 0
    fx_quotes: int = 0
    rates_written: int = 0
    fx_failed: bool = False


def household_ids(db: Session) -> list[uuid.UUID]:
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def refresh_market_data(
    db: Session,
    *,
    prices: PriceProvider | None,
    fx: FxProvider | None,
    today: date | None = None,
) -> RefreshStats:
    """One run (module doc). Leaves the session without an RLS context."""
    stats = RefreshStats()
    if prices is None and fx is None:
        return stats
    day = today if today is not None else clock.utcnow().date()
    households = household_ids(db)
    stats.households = len(households)

    quotes: list[PriceQuote] = []
    if prices is not None:
        symbols: set[str] = set()
        for household_id in households:
            set_context(db, household_id)
            symbols |= household_symbols(db, household_id)
        set_context(db, None)
        stats.symbols = len(symbols)
        if symbols:
            quotes = prices.fetch(sorted(symbols), today=day)
        stats.quotes = len(quotes)

    rates: list[FxQuote] = []
    if fx is not None:
        try:
            rates = fx.fetch()
        except FxProviderError as exc:
            stats.fx_failed = True
            log.warning("fx rate refresh failed", extra={"provider": fx.name, "error": str(exc)})
        stats.fx_quotes = len(rates)

    for household_id in households:
        set_context(db, household_id)
        if prices is not None and quotes:
            stats.prices_written += store_quotes(
                db, household_id, quotes, source=prices.name, today=day
            )
        if fx is not None and rates:
            stats.rates_written += store_reference_rates(
                db, household_id, rates, source=fx.name, today=day
            )
    set_context(db, None)
    log.info(
        "market data refreshed",
        extra={
            "households": stats.households,
            "symbols": stats.symbols,
            "quotes": stats.quotes,
            "prices_written": stats.prices_written,
            "fx_quotes": stats.fx_quotes,
            "rates_written": stats.rates_written,
        },
    )
    return stats


@job_handler(
    REFRESH_JOB_TYPE,
    payload=RefreshPricesPayload,
    timeout=REFRESH_TIMEOUT_SECONDS,
    max_attempts=1,
)
def refresh_prices(ctx: JobContext, payload: RefreshPricesPayload) -> None:
    settings = get_settings()
    if settings.price_provider == "none" and settings.fx_provider == "none":
        return  # the default: nothing is ever contacted
    if ctx.household_id is not None:
        raise PermanentJobError("refresh_prices is an instance-level job")
    try:
        prices = external_price_provider(settings)
    except PriceProviderConfigError as exc:
        raise PermanentJobError(str(exc)) from exc
    refresh_market_data(ctx.session, prices=prices, fx=external_fx_provider(settings))


recurring(REFRESH_JOB_TYPE, REFRESH_INTERVAL, RefreshPricesPayload())
