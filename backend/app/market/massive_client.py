"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time

from massive import RESTClient
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

NS_PER_SECOND = 1_000_000_000


def parse_snapshot(snap: TickerSnapshot) -> tuple[str, float, float] | None:
    """Extract (ticker, price, unix_seconds) from a snapshot, or None if unusable.

    Every SDK field is Optional: last_trade is None for thinly traded tickers and
    overnight (snapshots are cleared at 12am ET). Trade timestamps are nanoseconds.
    """
    trade = snap.last_trade
    if not snap.ticker or trade is None or trade.price is None or trade.price <= 0:
        return None
    ts_ns = trade.sip_timestamp or snap.updated
    timestamp = ts_ns / NS_PER_SECOND if ts_ns else time.time()
    return snap.ticker, float(trade.price), timestamp


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.
    The SDK is synchronous, so the call runs in a worker thread.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s

    The snapshot endpoint needs a Starter plan or above; a free Basic key gets
    a 403 on every poll (logged, prices never appear).
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        start_timeout: float = 10.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._start_timeout = start_timeout
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(t.strip().upper() for t in tickers))

        # Immediate first poll so the cache is warm, but never block startup for long
        try:
            await asyncio.wait_for(self._poll_once(), timeout=self._start_timeout)
        except TimeoutError:
            logger.warning("Massive: initial poll timed out; continuing in background")

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(self._tickers),
            self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        # Snapshot the list on the event loop; add_ticker may mutate it meanwhile
        tickers = list(self._tickers)
        if not tickers or not self._client:
            return

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except Exception as e:
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 403 (plan), 429 (rate limit), network errors.
            logger.error("Massive poll failed: %s", e)
            return

        # A ticker removed while the request was in flight must not reappear
        tracked = set(self._tickers)
        updated = 0
        for snap in snapshots:
            parsed = parse_snapshot(snap)
            if parsed is None:
                logger.debug("Massive: no usable trade for %s", getattr(snap, "ticker", "???"))
                continue
            ticker, price, timestamp = parsed
            if ticker in tracked:
                self._cache.update(ticker=ticker, price=price, timestamp=timestamp)
                updated += 1
        logger.debug("Massive poll: updated %d/%d tickers", updated, len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list[TickerSnapshot]:
        """Synchronous call to the Massive REST API. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
