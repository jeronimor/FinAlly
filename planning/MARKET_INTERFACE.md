# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations (simulator and Massive API) behind one abstract interface. All downstream code — SSE streaming, price cache, portfolio valuation — is source-agnostic.

## Core Data Model

```python
from dataclasses import dataclass

@dataclass
class PriceUpdate:
    """A single price update for one ticker."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds
    change: float             # price - previous_price
    direction: str            # "up", "down", or "flat"
```

This is the only data structure that leaves the market data layer. Everything downstream works with `PriceUpdate` objects.

## Abstract Interface

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Abstract interface for market data providers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing price updates and clean up."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of active tickers."""
```

Both implementations write to a shared `PriceCache` (see below). The interface does **not** return prices directly — it pushes updates into the cache on its own schedule.

## Price Cache

Shared in-memory store that both data sources write to and the SSE streamer reads from.

```python
import time
from threading import Lock

class PriceCache:
    """Thread-safe cache of latest prices per ticker."""

    def __init__(self):
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Update price for a ticker. Returns the PriceUpdate."""
        with self._lock:
            ts = timestamp or time.time()
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price

            if price > previous_price:
                direction = "up"
            elif price < previous_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                timestamp=ts,
                change=price - previous_price,
                direction=direction,
            )
            self._prices[ticker] = update
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get latest price for a ticker."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Get all current prices."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache."""
        with self._lock:
            self._prices.pop(ticker, None)
```

## Factory Function

Select the data source at startup based on environment:

```python
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        poll_interval = float(os.environ.get("MASSIVE_POLL_INTERVAL", "15"))
        return MassiveDataSource(
            api_key=api_key, price_cache=price_cache, poll_interval=poll_interval
        )
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

**Plan requirement:** the Massive source polls the snapshot endpoint, which the free **Basic** plan does **not** include. A Basic key gets a non-200 response on every poll, so no prices arrive. Live Massive data needs **Starter** or higher; Starter and Developer data is 15 minutes delayed, and only Advanced gets real-time data (see `MASSIVE_API.md`). The factory can't detect the plan from the key, so a free key still selects Massive. Users on the free plan should leave `MASSIVE_API_KEY` empty and use the simulator.

`MASSIVE_POLL_INTERVAL` defaults to 15s, which is 4 snapshot calls/min. Paid plans have unlimited calls and can poll every 2–5s.

## Massive Implementation Sketch

```python
import asyncio
import logging
from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

logger = logging.getLogger(__name__)

class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._client = RESTClient(api_key=api_key)  # raises AuthError if key is empty
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll so the cache isn't empty
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        try:
            # The SDK is synchronous (urllib3) — run it in a thread
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=self._tickers,
            )
        except BadResponse as e:
            # Any non-200 after the SDK's 3 built-in retries:
            # 401 bad key, 403 plan lacks snapshots (free Basic plan), 429 rate limit
            logger.error("Massive poll failed: %s", e)
            return

        for snap in snapshots:
            trade = snap.last_trade
            # Every SDK field is Optional; last_trade can be None overnight
            # (snapshots reset at 12am ET) or for thinly traded tickers
            if trade is None or trade.price is None or trade.sip_timestamp is None:
                continue
            self._cache.update(
                ticker=snap.ticker,
                price=trade.price,
                timestamp=trade.sip_timestamp / 1e9,  # nanoseconds -> seconds
            )
```

**Field names matter.** `LastTrade` has no `timestamp` attribute. The trade time is `sip_timestamp`, in **nanoseconds**. Aggregate bars (`day`, `prev_day`, `min`) use milliseconds instead. Tests must not build snapshots from a bare `MagicMock()`, because it accepts any attribute name and would hide this kind of error. Use `MagicMock(spec=...)` with the real SDK models, or construct `TickerSnapshot.from_dict(...)` from raw JSON.

## Simulator Implementation Sketch

```python
import asyncio

class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._sim: GBMSimulator | None = None  # See MARKET_SIMULATOR.md

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._sim = GBMSimulator(tickers=self._tickers)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            self._sim.add_ticker(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run_loop(self) -> None:
        while True:
            prices = self._sim.step()  # Returns dict[str, float]
            for ticker, price in prices.items():
                self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

## Source Differences Downstream Code Must Tolerate

Both sources write the same `PriceUpdate`, but they behave differently:

| | Simulator | Massive |
|---|---|---|
| Update cadence | ~500ms | Once per `MASSIVE_POLL_INTERVAL` (default 15s) |
| New ticker priced | Immediately (seed price) | On the next poll; unknown symbols stay unpriced |
| `timestamp` meaning | Wall-clock time of the tick (`time.time()`) | Exchange time of the last trade (`sip_timestamp / 1e9`). Can lag wall-clock time by 15 min on Starter/Developer, or by hours when the market is closed |
| Missing data | Never | A ticker can be absent from the response or have `last_trade=None`. Its previous cached price is kept |
| Failure mode | None | `BadResponse` is logged and the cache keeps its last values |
| Plan needed | — | Starter+ (snapshots aren't on the free Basic plan) |

So:
- Never assume every tracked ticker has a cached price. Use the "No price available" path in trade execution and show `null` / "—" in the UI
- Don't compute staleness by comparing `timestamp` with `time.time()`, because Massive timestamps are legitimately old outside market hours

## Integration with SSE

The SSE endpoint reads from the `PriceCache` and pushes to connected clients:

```python
async def price_stream(price_cache: PriceCache):
    """SSE generator that yields price updates."""
    while True:
        prices = price_cache.get_all()
        data = {
            ticker: {
                "ticker": p.ticker,
                "price": p.price,
                "previous_price": p.previous_price,
                "change": p.change,
                "direction": p.direction,
                "timestamp": p.timestamp,
            }
            for ticker, p in prices.items()
        }
        yield f"data: {json.dumps(data)}\n\n"
        await asyncio.sleep(0.5)
```

## File Structure

```
backend/
  app/
    market/
      __init__.py
      models.py             # PriceUpdate dataclass
      interface.py           # MarketDataSource ABC, PriceCache
      factory.py             # create_market_data_source()
      massive_client.py      # MassiveDataSource
      simulator.py           # SimulatorDataSource + GBMSimulator
      seed_prices.py         # Default ticker seed prices
```

## Lifecycle

1. **App startup**: Create `PriceCache`, call `create_market_data_source(price_cache)`, then `await source.start(initial_tickers)`
2. **Watchlist changes**: Call `source.add_ticker()` or `source.remove_ticker()`
3. **SSE streaming**: Reads from `PriceCache.get_all()` every 500ms
4. **Trade execution**: Reads current price from `PriceCache.get(ticker)`
5. **App shutdown**: Call `await source.stop()`
